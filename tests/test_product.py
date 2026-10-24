"""Contracts for the v0.1 integrations, against real processes and kernel rules."""

import json
import os
import pty
import select
import socket
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import test_runtime as runtime


def run(*args, cwd=None, env=None, stdin=None):
    return subprocess.run(
        [str(runtime.BINARY), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
        env=env,
        input=stdin,
    )


class DefaultModeTests(unittest.TestCase):
    """`pynox script.py` is sandboxed Python with Python's output and exit codes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pynox-product-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, code):
        path = self.root / name
        path.write_text(textwrap.dedent(code))
        return path

    def test_output_exit_code_and_arguments_are_pythons(self):
        script = self.write("plain.py", "import sys\nprint('plain', sys.argv[1:])\nsys.exit(3)\n")
        result = run(script, "--allow-network", "x")
        self.assertEqual(
            (result.returncode, result.stdout), (3, "plain ['--allow-network', 'x']\n")
        )
        self.assertEqual(result.stderr, "")
        result = run("-c", "import sys; print(sys.argv, __name__); sys.exit(7)", "one")
        self.assertEqual((result.returncode, result.stdout), (7, "['-c', 'one'] __main__\n"))
        result = run("-m", "json.tool", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"usage: python[0-9.]* -m json.tool")

    def test_sandbox_applies_with_traceback_and_hint_on_stderr(self):
        script = self.write("probe.py", "open('/etc/passwd').read()\n")
        result = run(script)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Traceback (most recent call last):", result.stderr)
        self.assertIn('probe.py", line 1', result.stderr)
        self.assertNotIn("pynox-bootstrap", result.stderr)
        self.assertIn("PermissionError: [Errno 13] Permission denied: '/etc/passwd'", result.stderr)
        self.assertIn("pynox: For files, grant the specific path", result.stderr)
        result = run("--allow-read=/etc/passwd", script)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_directory_is_readable_and_nothing_else(self):
        self.write("helper.py", "VALUE = 'sibling import works'\n")
        (self.root / "data.txt").write_text("sibling file")
        script = self.write(
            "main.py",
            "import helper\nprint(helper.VALUE)\nprint(open(__file__.replace('main.py', 'data.txt')).read())\n",
        )
        result = run(script)
        self.assertEqual(result.stdout, "sibling import works\nsibling file\n", result.stderr)
        with tempfile.TemporaryDirectory(prefix="pynox-elsewhere-") as elsewhere:
            (Path(elsewhere) / "secret.txt").write_text("private")
            denied = self.write(
                "reader.py", f"open({str(Path(elsewhere) / 'secret.txt')!r}).read()\n"
            )
            self.assertEqual(run(denied).returncode, 1)
            # -c grants nothing implicitly, not even the current directory.
            result = run("-c", "open('secret.txt').read()", cwd=elsewhere)
            self.assertEqual(result.returncode, 1)
            self.assertIn("PermissionError", result.stderr)
            # -m resolves modules from the current directory, so it may read it.
            (Path(elsewhere) / "mod.py").write_text("print(open('secret.txt').read())\n")
            result = run("-m", "mod", cwd=elsewhere)
            self.assertEqual((result.returncode, result.stdout), (0, "private\n"), result.stderr)

    def test_interpreter_options_need_no_sandbox(self):
        result = run("-O", "-c", "pass")
        self.assertEqual(result.returncode, 125)
        self.assertIn("use --no-sandbox", result.stderr)
        result = run("-I", "-u", "-c", "print('isolation flags are accepted')")
        self.assertEqual(result.stdout, "isolation flags are accepted\n", result.stderr)
        result = run()
        self.assertEqual(result.returncode, 125)
        self.assertIn("REPL needs --no-sandbox", result.stderr)

    def test_stdin_passes_through(self):
        result = run("-c", "import sys; print(sys.stdin.read().strip().upper())", stdin="hello\n")
        self.assertEqual(result.stdout, "HELLO\n", result.stderr)

    def test_stdout_is_buffered_for_pipes_and_files_stderr_is_line_buffered(self):
        code = (
            "import sys\nprint('buffered')\n"
            "sys.stderr.write('ready\\n')\n"
            "sys.stdin.readline()\nprint('released', end='')\n"
        )
        for destination in ("pipe", "file"):
            with self.subTest(destination=destination):
                with (self.root / "stdout.txt").open("w+b") as output_file:
                    process = subprocess.Popen(
                        [str(runtime.BINARY), "-c", code],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE if destination == "pipe" else output_file,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        self.assertTrue(select.select([process.stderr], [], [], 5)[0])
                        self.assertEqual(os.read(process.stderr.fileno(), 1024), b"ready\n")
                        if destination == "pipe":
                            self.assertFalse(select.select([process.stdout], [], [], 0)[0])
                        else:
                            self.assertEqual(os.fstat(output_file.fileno()).st_size, 0)
                        output, errors = process.communicate(b"\n", timeout=5)
                        self.assertEqual(process.returncode, 0, errors)
                        self.assertEqual(errors, b"")
                        if destination == "file":
                            output_file.seek(0)
                            output = output_file.read()
                        self.assertEqual(output, b"buffered\nreleased")
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)

    def test_terminal_lines_and_input_prompt_are_visible_before_input(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        code = (
            "import sys\nprint('line ready')\nsys.stdin.readline()\n"
            "name = input('Name: ')\nprint('Hello ' + name)\n"
        )
        process = subprocess.Popen(
            [str(runtime.BINARY), "-c", code],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        transcript = b""

        def read_until(expected):
            nonlocal transcript
            deadline = time.monotonic() + 5
            while expected not in transcript:
                remaining = max(0, deadline - time.monotonic())
                self.assertTrue(select.select([master], [], [], remaining)[0], transcript)
                transcript += os.read(master, 4096)

        try:
            read_until(b"line ready\r\n")
            os.write(master, b"\n")
            read_until(b"Name: ")
            os.write(master, b"world\n")
            read_until(b"Hello world\r\n")
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, transcript)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_timeout_and_setup_failures_are_reported_on_stderr(self):
        script = self.write("loop.py", "while True: pass\n")
        result = run("--timeout=200ms", script)
        self.assertEqual(result.returncode, 124)
        self.assertIn("pynox: TimeoutError: Wall time exceeded 200ms", result.stderr)
        result = run("--allow-read=/definitely/missing", script)
        self.assertEqual(result.returncode, 125)
        self.assertIn("Permission path must exist", result.stderr)


class NoSandboxTests(unittest.TestCase):
    def test_no_sandbox_is_the_cpython_cli(self):
        result = run("--no-sandbox", "-c", "import sys; print(sys.argv[1:]); sys.exit(7)", "one")
        self.assertEqual((result.returncode, result.stdout), (7, "['one']\n"), result.stderr)
        result = run("--no-sandbox", "-O", "-c", "print(__debug__)")
        self.assertEqual(result.stdout, "False\n", result.stderr)
        result = run("--no-sandbox", "-c", "print(open('/etc/passwd').readline() != '')")
        self.assertEqual(result.stdout, "True\n", result.stderr)
        for bad in (
            ["--no-sandbox", "--json", "x.py"],
            ["--no-sandbox", "--allow-network", "x.py"],
        ):
            result = run(*bad)
            self.assertEqual(result.returncode, 2, bad)
            self.assertIn("--no-sandbox cannot be combined", result.stderr)

    def test_optimizations_are_transparent_and_lazy(self):
        result = run(
            "--no-sandbox",
            "-c",
            "import sys\n"
            "before = '_pynox_json_runtime' in sys.modules, 'json' in sys.modules\n"
            "import json\n"
            "after = '_pynox_json_runtime' in sys.modules\n"
            "print(before, after, json.dump.__module__, type(json.__loader__).__name__,\n"
            "      json.__spec__.loader is json.__loader__, json.__spec__.name)\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "(False, False) True json SourceFileLoader True json\n")

    def test_doctor_version_and_help(self):
        doctor = json.loads(subprocess.check_output([runtime.BINARY, "doctor"]))
        self.assertEqual(doctor["runtime"], "pynox")
        python = run("--no-sandbox", "-c", "import platform; print(platform.python_version())")
        expected = f"pynox {doctor['version']} (CPython {python.stdout.strip()})\n"
        for flag in ("--version", "-V"):
            with self.subTest(flag=flag):
                result = run(flag)
                self.assertEqual((result.stdout, result.stderr), (expected, ""))
        result = run("--help")
        self.assertIn("--no-sandbox", result.stdout)
        self.assertIn("pynox.toml", result.stdout)


class JsonContractTests(unittest.TestCase):
    def stock_results(self):
        source = runtime.ROOT / "tests/fixtures/json_contract.py"
        doctor = json.loads(subprocess.check_output([runtime.BINARY, "doctor"]))
        base = subprocess.run(
            [doctor["python"]["executable"], "-I", "-S", "-B", source],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        expected = json.loads(base.stdout)["results"]
        self.assertEqual(len(expected), 158)
        return source, expected

    def test_json_differential_without_sandbox(self):
        source, expected = self.stock_results()
        result = run("--no-sandbox", "-I", "-S", "-B", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(expected, actual["results"])
        self.assertEqual(actual["paths"]["nested"]["fast"], 1)
        self.assertEqual(actual["paths"]["writer_custom"]["fallback"], 1)

    def test_json_differential_under_sandbox(self):
        source, expected = self.stock_results()
        with tempfile.TemporaryDirectory() as temporary:
            completed = run(
                "--json",
                "--max-output=1048576",
                f"--allow-read={temporary}",
                f"--allow-write={temporary}",
                "--allow-env=TMPDIR",
                source,
                env={**os.environ, "TMPDIR": temporary},
            )
        report = json.loads(completed.stdout)
        self.assertEqual(report["exit"], 0, report)
        actual = json.loads(report["stdout"])
        # Partial output before a RecursionError depends on the frames above the
        # script; the sandbox runner adds one. The error itself must match.
        depth_dependent = {"cycle_no_check"}
        for name in depth_dependent:
            self.assertEqual(expected[name]["error"], actual["results"][name]["error"])
        self.assertEqual(
            {k: v for k, v in expected.items() if k not in depth_dependent},
            {k: v for k, v in actual["results"].items() if k not in depth_dependent},
        )
        self.assertEqual(actual["paths"]["nested"]["fast"], 1)

    def test_sandbox_uses_utf8_like_stock_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "test.py"
            script.write_text(
                "import sys\n"
                f"with open({str(root / 'out.txt')!r}, 'w') as f: f.write('ação')\n"
                "print(sys.getfilesystemencoding(), sys.stdout.encoding, 'ação')\n"
            )
            result = run("--json", f"--allow-write={root}", script)
            report = json.loads(result.stdout)
            self.assertEqual(report["exit"], 0, report)
            self.assertEqual(report["stdout"], "utf-8 utf-8 ação\n")
            self.assertEqual((root / "out.txt").read_bytes(), "ação".encode())

    def test_json_result_describes_the_effective_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "s.py"
            script.write_text("print('ok')\n")
            report = json.loads(run("--json", "--timeout=2s", script).stdout)
        self.assertEqual((report["exit"], report["stdout"]), (0, "ok\n"))
        self.assertEqual(report["policy"]["implicit_read"], [str(script.parent.resolve())])
        self.assertEqual(report["policy"]["timeout_ms"], 2000)
        self.assertEqual(report["runtime"]["output"], "captured")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pynox-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "input.txt").write_text("configured read")
        self.out = self.root / "out"
        self.out.mkdir()
        self.script = self.root / "scripts" / "job.py"
        self.script.parent.mkdir()
        self.script.write_text(
            f"print(open({str(self.data / 'input.txt')!r}).read())\n"
            f"open({str(self.out / 'result.txt')!r}, 'w').write('written')\n"
        )
        self.env = {k: v for k, v in os.environ.items() if k != "PYNOX_CONFIG"}

    def test_pynox_toml_in_the_current_directory_grants_permissions(self):
        (self.root / "pynox.toml").write_text(
            '[permissions]\nallow_read = ["data"]\nallow_write = ["out"]\n'
        )
        denied = run(self.script, cwd=self.root / "scripts", env=self.env)
        self.assertEqual(denied.returncode, 1, "a file beside the script must not count")
        result = run(self.script, cwd=self.root, env=self.env)
        self.assertEqual(
            (result.returncode, result.stdout), (0, "configured read\n"), result.stderr
        )
        self.assertEqual((self.out / "result.txt").read_text(), "written")

    def test_explicit_config_env_var_and_profiles(self):
        config = self.root / "etc" / "prod.toml"
        config.parent.mkdir()
        config.write_text(
            '[permissions]\nallow_read = ["../data"]\n'
            '[profiles.writer]\nallow_write = ["../out"]\ntimeout = "9s"\n'
        )
        result = run(self.script, "--config", config, env=self.env)
        self.assertEqual(result.returncode, 1, "profile not applied, write must fail")
        result = run("--json", f"--config={config}", "--profile=writer", self.script, env=self.env)
        report = json.loads(result.stdout)
        self.assertEqual((report["exit"], report["stdout"]), (0, "configured read\n"), report)
        self.assertEqual(report["policy"]["timeout_ms"], 9000)
        self.assertEqual(report["policy"]["allow_write"], [str(self.out.resolve())])
        result = run("--profile=writer", self.script, env={**self.env, "PYNOX_CONFIG": str(config)})
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run(
            "--profile=missing", self.script, env={**self.env, "PYNOX_CONFIG": str(config)}
        )
        self.assertEqual(result.returncode, 125)
        self.assertIn("Unknown profile", result.stderr)
        result = run("--profile=writer", self.script, env=self.env)
        self.assertIn("--profile needs a pynox.toml", result.stderr)

    def test_configuration_inside_a_writable_path_is_refused(self):
        (self.root / "pynox.toml").write_text('[permissions]\nallow_write = ["."]\n')
        result = run(self.script, cwd=self.root, env=self.env)
        self.assertEqual(result.returncode, 125)
        self.assertIn("inside writable path", result.stderr)
        (self.root / "pynox.toml").write_text('[permissions]\nallow_reads = ["data"]\n')
        result = run(self.script, cwd=self.root, env=self.env)
        self.assertEqual(result.returncode, 125)
        self.assertIn("Invalid configuration", result.stderr)

    def test_pool_profiles_and_limits(self):
        config = self.root / "pool.toml"
        config.write_text(
            "[pool]\nworkers = 1\n"
            '[pool.limits]\nallow_read = ["data"]\nallow_write = ["out"]\n'
            'allow_network = false\ntimeout = "10s"\n'
            '[profiles.analysis]\nallow_read = ["data"]\nallow_write = ["out"]\n'
        )
        requests = [
            {"id": "profile", "profile": "analysis", "script": str(self.script)},
            {"id": "bare", "script": str(self.script)},
            {"id": "over-read", "code": "pass", "allow_read": [str(self.root)]},
            {"id": "network", "code": "pass", "allow_network": True},
            {"id": "slow", "code": "pass", "timeout": "11s"},
            {"id": "within", "code": "pass", "timeout": "10s", "allow_write": [str(self.out)]},
            {"id": "unknown-profile", "profile": "nope", "code": "pass"},
        ]
        result = run(
            "pool",
            f"--config={config}",
            stdin="".join(json.dumps(r) + "\n" for r in requests),
            env=self.env,
        )
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        ready, reports = lines[0], {r["id"]: r for r in lines[1:]}
        self.assertEqual((ready["workers"], ready["profiles"]), (1, ["analysis"]), ready)
        self.assertEqual(reports["profile"]["stdout"], "configured read\n", reports["profile"])
        self.assertEqual(reports["bare"]["error"]["type"], "PermissionError")
        for name in ("over-read", "network", "slow", "unknown-profile"):
            self.assertEqual(reports[name]["error"]["type"], "RequestError", reports[name])
            self.assertIn(
                "limits" if name != "unknown-profile" else "profile",
                reports[name]["error"]["message"],
            )
        self.assertEqual(reports["within"]["exit"], 0, reports["within"])
        result = run(
            "pool", stdin='{"id": 1, "profile": "analysis", "code": "pass"}\n', env=self.env
        )
        report = json.loads(result.stdout.splitlines()[1])
        self.assertIn("--config", report["error"]["message"])


class PoolTests(unittest.TestCase):
    def test_pool_optimization_is_present_in_every_job_and_stays_lazy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lazy = root / "lazy.py"
            lazy.write_text("import sys\nprint('_pynox_json_runtime' in sys.modules)\n")
            eager = root / "eager.py"
            eager.write_text("import sys, json\nprint('_pynox_json_runtime' in sys.modules)\n")
            requests = [{"script": str(path)} for path in (lazy, eager, lazy, eager)]
            result = run(
                "pool",
                "--workers=1",
                stdin="".join(json.dumps(r) + "\n" for r in requests),
            )
            reports = [json.loads(line) for line in result.stdout.splitlines()][1:]
            self.assertEqual(
                [r["stdout"] for r in reports], ["False\n", "True\n", "False\n", "True\n"]
            )

    def test_pool_runs_inline_code_without_files(self):
        requests = [
            {
                "id": "named",
                "code": "import sys\nprint(sys.argv, __name__, '__file__' in dir())",
                "name": "step.py",
                "args": ["a"],
            },
            {"id": "error", "code": "x = 1\n1/0\n", "name": "calc.py"},
            {"id": "default-name", "code": "raise ValueError('boom')"},
            {
                "id": "no-file-grant",
                "code": "open(__import__('sys').argv[0]).read()",
                "name": "step.py",
            },
            {"id": "both", "script": "a.py", "code": "print(1)"},
            {"id": "neither"},
        ]
        result = run("pool", "--workers=1", stdin="".join(json.dumps(r) + "\n" for r in requests))
        reports = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()[1:])}
        self.assertEqual(reports["named"]["stdout"], "['step.py', 'a'] __main__ False\n")
        self.assertEqual(reports["named"]["exit"], 0, reports["named"])
        error = reports["error"]["error"]
        self.assertEqual(
            (error["type"], error["file"], error["line"]), ("ZeroDivisionError", "calc.py", 2)
        )
        self.assertEqual(reports["default-name"]["error"]["file"], "<code>")
        self.assertEqual(reports["no-file-grant"]["error"]["type"], "FileNotFoundError")
        self.assertEqual(reports["both"]["error"]["type"], "RequestError")
        self.assertEqual(reports["neither"]["error"]["type"], "RequestError")

    def test_network_permission_reaches_local_server(self):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as temporary:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(2)
            script = Path(temporary) / "client.py"
            script.write_text(
                "import socket\n"
                f"with socket.create_connection({listener.getsockname()!r}) as s:\n"
                "    s.sendall(b'pynox')\n"
            )
            result = run("--allow-network", script)
            self.assertEqual(result.returncode, 0, result.stderr)
            connection, _ = listener.accept()
            with connection:
                self.assertEqual(connection.recv(32), b"pynox")


class FileSizeLimitTests(unittest.TestCase):
    def test_writes_beyond_the_limit_fail_with_an_exception(self):
        with tempfile.TemporaryDirectory(prefix="pynox-fsize-") as temporary:
            root = Path(temporary)
            code = (
                "import errno\n"
                f"f = open({str(root / 'big.bin')!r}, 'wb', buffering=0)\n"
                "try:\n"
                "    for _ in range(10):\n"
                "        f.write(b'x' * 60000)\n"
                "except OSError as e:\n"
                "    print('stopped', errno.errorcode[e.errno])\n"
            )
            result = run(f"--allow-write={root}", "--max-file-size=64k", "-c", code)
            self.assertEqual(
                (result.returncode, result.stdout), (0, "stopped EFBIG\n"), result.stderr
            )
            self.assertLessEqual((root / "big.bin").stat().st_size, 64 * 1024)
            report = json.loads(run("--json", "--max-file-size=64k", "-c", "pass").stdout)
            self.assertEqual(report["policy"]["max_file_size_bytes"], 64 * 1024)
            unlimited = run(
                f"--allow-write={root}",
                "-c",
                f"open({str(root / 'free.bin')!r}, 'wb').write(b'x' * 200000)",
            )
            self.assertEqual(unlimited.returncode, 0, unlimited.stderr)

    def test_pool_limit_applies_to_requests_that_do_not_set_one(self):
        with tempfile.TemporaryDirectory(prefix="pynox-fsize-") as temporary:
            root = Path(temporary)
            (root / "out").mkdir()
            config = root / "pynox.toml"
            config.write_text(
                '[pool]\nworkers = 1\n[pool.limits]\nallow_write = ["out"]\nmax_file_size = "64k"\n'
            )
            target = root / "out" / "f.bin"
            # CPython 3.14+ may defer the error from a buffered write to close().
            write = f"with open({str(target)!r}, 'wb') as f:\n    f.write(b'x' * 100000)"
            requests = [
                {"id": "default", "code": write, "allow_write": [str(root / "out")]},
                {"id": "over", "code": "pass", "max_file_size": "1m"},
                {"id": "under", "code": "pass", "max_file_size": "8k"},
            ]
            result = run(
                "pool", f"--config={config}", stdin="".join(json.dumps(r) + "\n" for r in requests)
            )
            reports = {r["id"]: r for r in map(json.loads, result.stdout.splitlines()[1:])}
            self.assertEqual(reports["default"]["error"]["type"], "OSError", reports["default"])
            self.assertEqual(reports["default"]["policy"]["max_file_size_bytes"], 64 * 1024)
            self.assertEqual(reports["over"]["error"]["type"], "RequestError")
            self.assertEqual(reports["under"]["policy"]["max_file_size_bytes"], 8 * 1024)


class ObservabilityTests(unittest.TestCase):
    def test_stats_count_outcomes_and_logs_describe_jobs_without_output(self):
        jobs = [
            {"id": "ok", "code": "print('secret-value')"},
            {"id": "err", "code": "1/0"},
            {"id": "denied", "code": "open('/etc/passwd').read()"},
            {"id": "slow", "code": "while True: pass", "timeout": "200ms"},
            {"id": "bad", "code": "pass", "timeout": "forever"},
            {"id": "s", "session": "obs", "code": "x = 1"},
        ]
        process = subprocess.Popen(
            [str(runtime.BINARY), "pool", "--workers=1", "--log=json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.kill)
        process.stdout.readline()
        for job in jobs:
            process.stdin.write(json.dumps(job) + "\n")
        process.stdin.flush()
        answered = {json.loads(process.stdout.readline())["id"] for _ in jobs}
        self.assertEqual(answered, {job["id"] for job in jobs})
        process.stdin.write(json.dumps({"id": "stats", "stats": True}) + "\n")
        process.stdin.flush()
        stats = json.loads(process.stdout.readline())
        process.stdin.close()
        stderr = process.stderr.read()
        process.wait(timeout=10)
        self.assertEqual((stats["id"], stats["event"], stats["workers"]), ("stats", "stats", 1))
        self.assertEqual(stats["sessions_active"], 1)
        self.assertEqual((stats["jobs"], stats["ok"], stats["failed"]), (6, 2, 2))
        self.assertEqual((stats["timeouts"], stats["request_errors"]), (1, 1))
        self.assertEqual(stats["errors"]["ZeroDivisionError"], 1)
        self.assertEqual(stats["errors"]["PermissionError"], 1)
        logs = [json.loads(line) for line in stderr.splitlines()]
        self.assertEqual(len(logs), 6, stderr)
        by_id = {entry["id"]: entry for entry in logs}
        self.assertEqual((by_id["ok"]["exit"], by_id["ok"]["error"]), (0, None))
        self.assertEqual(by_id["denied"]["error"], "PermissionError")
        self.assertEqual(by_id["slow"]["exit"], 124)
        self.assertEqual(by_id["s"]["session"], "obs")
        self.assertNotIn("secret-value", stderr, "logs never carry captured output")

    def test_logging_is_off_by_default_and_validated(self):
        quiet = run("pool", "--workers=1", stdin='{"id": 1, "code": "pass"}\n')
        self.assertEqual(quiet.stderr, "")
        bad = run("pool", "--workers=1", "--log=xml", stdin="")
        self.assertEqual(bad.returncode, 125)
        self.assertIn("log must be", bad.stdout)
