"""Persistent sessions: one isolated process per session, steps share __main__."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import test_runtime as runtime

sys.path.insert(0, str(runtime.ROOT / "python"))
os.environ["PYNOX_BIN"] = str(runtime.BINARY)

import pynox  # noqa: E402


def pool_dialog(requests, *args, timeout=60):
    result = subprocess.run(
        [str(runtime.BINARY), "pool", *args],
        input="".join(json.dumps(r) + "\n" for r in requests),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    return lines[0], {r["id"]: r for r in lines[1:]}


class SessionProtocolTests(unittest.TestCase):
    def test_buffered_output_belongs_to_its_step_even_on_error(self):
        _, reports = pool_dialog(
            [
                {
                    "id": "a",
                    "session": "s",
                    "code": "import sys\nprint('first', end='')\n"
                    "print('diagnostic', end='', file=sys.stderr)\nraise ValueError('oops')",
                },
                {"id": "b", "session": "s", "code": "print('second', end='')"},
                {"id": "c", "session": "s", "code": "pass"},
                {"id": "d", "session": "s", "close": True},
            ],
            "--workers=1",
        )
        self.assertEqual(reports["a"]["error"]["type"], "ValueError")
        self.assertEqual(reports["a"]["stdout"], "first")
        self.assertEqual(reports["a"]["stderr"], "diagnostic")
        self.assertEqual(reports["b"]["stdout"], "second")
        self.assertEqual(reports["b"]["stderr"], "")
        self.assertEqual(reports["c"]["stdout"], "")
        self.assertEqual(reports["c"]["stderr"], "")

    def test_state_persists_errors_do_not_end_it_and_close_works(self):
        ready, reports = pool_dialog(
            [
                {"id": "a", "session": "s1", "code": "import json\nx = 41\nprint('started')"},
                {"id": "b", "session": "s1", "code": "print(json.dumps({'x': x + 1}))"},
                {"id": "c", "session": "s1", "code": "1/0", "name": "step3.py"},
                {"id": "d", "session": "s1", "code": "print(x)"},
                {"id": "e", "session": "s1", "close": True},
                {"id": "f", "session": "s1", "code": "print('x' in dir())"},
                {"id": "g", "session": "s1", "close": True},
                {"id": "h", "session": "nope", "close": True},
            ],
            "--workers=1",
        )
        self.assertEqual(ready["sessions"], {"max": 16, "idle_ms": 600000})
        self.assertEqual(reports["a"]["stdout"], "started\n", reports["a"])
        self.assertEqual((reports["a"]["session"], reports["a"]["step"]), ("s1", 1))
        self.assertEqual(reports["a"]["runtime"]["mode"], "session")
        self.assertEqual(json.loads(reports["b"]["stdout"]), {"x": 42})
        self.assertEqual(reports["c"]["error"]["type"], "ZeroDivisionError")
        self.assertEqual(
            (reports["c"]["error"]["file"], reports["c"]["error"]["line"]), ("step3.py", 1)
        )
        self.assertEqual(reports["d"]["stdout"], "41\n", "an error keeps the session alive")
        self.assertEqual(reports["d"]["step"], 4)
        self.assertTrue(reports["e"]["closed"])
        self.assertEqual(reports["f"]["stdout"], "False\n", "a new session after close")
        self.assertEqual(reports["f"]["step"], 1)
        self.assertEqual(reports["h"]["error"]["type"], "RequestError")

    def test_permissions_are_fixed_by_the_first_request(self):
        with tempfile.TemporaryDirectory(prefix="pynox-session-") as temporary:
            target = Path(temporary) / "out.txt"
            _, reports = pool_dialog(
                [
                    {
                        "id": "a",
                        "session": "w",
                        "code": f"open({str(target)!r}, 'w').write('1')",
                        "allow_write": [temporary],
                    },
                    {"id": "b", "session": "w", "code": f"open({str(target)!r}, 'a').write('2')"},
                    {"id": "c", "session": "w", "code": "pass", "allow_read": ["/etc"]},
                    {"id": "d", "session": "w", "code": "print(open('/etc/hostname').read())"},
                    {"id": "e", "session": "w", "script": "x.py"},
                    {
                        "id": "f",
                        "session": "w",
                        "code": "print('still here')",
                        "timeout": "2s",
                        "max_output": 10,
                    },
                ],
                "--workers=1",
            )
            self.assertTrue(
                reports["a"]["exit"] == 0 and reports["b"]["exit"] == 0,
                (reports["a"], reports["b"]),
            )
            self.assertEqual(target.read_text(), "12")
            self.assertEqual(reports["c"]["error"]["type"], "RequestError")
            self.assertIn("fixed by its first request", reports["c"]["error"]["message"])
            self.assertEqual(reports["d"]["error"]["type"], "PermissionError")
            self.assertEqual(reports["e"]["error"]["type"], "RequestError")
            self.assertEqual(reports["f"]["stdout"], "still here")
            self.assertTrue(reports["f"]["stdout_truncated"])

    def test_timeout_and_memory_errors_end_the_session(self):
        _, reports = pool_dialog(
            [
                {"id": "a", "session": "t", "code": "x = 1", "timeout": "1s"},
                {"id": "b", "session": "t", "code": "while True: pass", "timeout": "300ms"},
                {"id": "c", "session": "t", "code": "print('x' in dir())"},
                {"id": "d", "session": "m", "code": "import os; os._exit(3)"},
                {"id": "e", "session": "m", "code": "print('fresh')"},
            ],
            "--workers=1",
        )
        self.assertEqual(
            (reports["b"]["exit"], reports["b"]["error"]["type"]), (124, "TimeoutError")
        )
        self.assertTrue(reports["b"]["session_ended"])
        self.assertEqual(reports["c"]["stdout"], "False\n", "a new session replaced the killed one")
        self.assertEqual(reports["d"]["error"]["type"], "WorkerError")
        self.assertTrue(reports["d"]["session_ended"])
        self.assertEqual(reports["e"]["stdout"], "fresh\n")

    def test_sessions_run_beside_ordinary_jobs_and_respect_limits(self):
        with tempfile.TemporaryDirectory(prefix="pynox-session-") as temporary:
            config = Path(temporary) / "pynox.toml"
            config.write_text('[pool]\nworkers = 1\nmax_sessions = 2\nsession_idle = "1s"\n')
            ready, reports = pool_dialog(
                [
                    {"id": "s1", "session": "one", "code": "n = 1"},
                    {"id": "s2", "session": "two", "code": "n = 2"},
                    {"id": "s3", "session": "three", "code": "n = 3"},
                    {"id": "job", "code": "print('plain job')"},
                    {"id": "s1b", "session": "one", "code": "print(n)"},
                ],
                f"--config={config}",
            )
            self.assertEqual(ready["sessions"], {"max": 2, "idle_ms": 1000})
            self.assertEqual(reports["s3"]["error"]["type"], "RequestError")
            self.assertIn("limit", reports["s3"]["error"]["message"])
            self.assertEqual(reports["job"]["stdout"], "plain job\n")
            self.assertEqual(reports["s1b"]["stdout"], "1\n")

    def test_idle_sessions_close_themselves(self):
        with tempfile.TemporaryDirectory(prefix="pynox-session-") as temporary:
            config = Path(temporary) / "pynox.toml"
            config.write_text('[pool]\nworkers = 1\nsession_idle = "500ms"\n')
            process = subprocess.Popen(
                [str(runtime.BINARY), "pool", f"--config={config}"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(process.kill)
            process.stdout.readline()

            def ask(request):
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            self.assertEqual(ask({"id": 1, "session": "idle", "code": "v = 'kept'"})["exit"], 0)
            self.assertEqual(
                ask({"id": 2, "session": "idle", "code": "print(v)"})["stdout"], "kept\n"
            )
            time.sleep(1.2)
            self.assertEqual(
                ask({"id": 3, "session": "idle", "code": "print('v' in dir())"})["stdout"],
                "False\n",
            )
            process.stdin.close()
            process.wait(timeout=10)


class SessionPackageTests(unittest.TestCase):
    def test_session_object_and_stateful_tool(self):
        with pynox.Pool(workers=1) as pool:
            with pool.session(timeout="5s") as session:
                self.assertTrue(session.run("import math\nradius = 2").ok)
                self.assertEqual(
                    session.run("print(math.pi * radius ** 2)").text.strip()[:5], "12.56"
                )
                failed = session.run("undefined_name")
                self.assertEqual(failed.error["type"], "NameError")
                self.assertTrue(session.alive)
                self.assertEqual(session.steps, 3)
            self.assertFalse(session.alive)
            notebook = pool.tool(session=True)
            self.assertIn("persist between calls", notebook.__doc__)
            self.assertEqual(notebook("total = 10"), "(no output)")
            self.assertEqual(notebook("total += 5\nprint(total)"), "15\n")
            stateless = pool.tool()
            self.assertIn("NameError", stateless("print(total)"))
