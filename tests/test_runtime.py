"""Behavioral tests against the actual kernel sandbox; no mocked permissions."""

import ctypes
import errno
import json
import os
import platform
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("PYNOX_BIN", ROOT / "target/release/pynox")).resolve()


def deny_landlock():
    """Inherited seccomp filter to simulate a host with Landlock unavailable."""

    class Instruction(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Instruction))]

    # Landlock create_ruleset is syscall 444 on both supported architectures.
    instructions = (Instruction * 4)(
        Instruction(0x20, 0, 0, 0),
        Instruction(0x15, 0, 1, 444),
        Instruction(0x06, 0, 0, 0x00050000 | errno.ENOSYS),
        Instruction(0x06, 0, 0, 0x7FFF0000),
    )
    program = Program(4, instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.byref(program), 0, 0):
        os._exit(99)


@unittest.skipUnless(platform.system() == "Linux", "Linux sandbox backend")
class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise RuntimeError("Build first: bash scripts/build.sh")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pynox-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = self.root / "secret"
        self.secret.write_text("private fixture")
        # The script's own directory is implicitly readable; keep the secret above it.
        (self.root / "app").mkdir()
        self.script = self.root / "app" / "main.py"

    def run_code(self, code, *flags, args=(), env=None, pass_fds=(), preexec_fn=None):
        self.script.write_text(textwrap.dedent(code))
        completed = subprocess.run(
            [str(BINARY), "--json", *map(str, flags), str(self.script), *args],
            text=True,
            capture_output=True,
            timeout=15,
            env=env,
            pass_fds=pass_fds,
            preexec_fn=preexec_fn,
        )
        self.assertEqual(completed.stderr, "", completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, result["exit"], result)
        return result

    def assert_denied(self, result):
        self.assertEqual(result["exit"], 1, result)
        self.assertEqual(result["error"]["type"], "PermissionError", result)

    def test_buffered_output_survives_completion_exception_and_sys_exit(self):
        for ending, exit_code, error in (
            ("", 0, None),
            ("raise ValueError('after printing')", 1, "ValueError"),
            ("sys.exit(7)", 7, None),
        ):
            with self.subTest(ending=ending):
                result = self.run_code(
                    "import sys\n"
                    "for i in range(3000): print(i)\n"
                    "print('stdout tail', end='')\n"
                    "print('stderr tail', end='', file=sys.stderr)\n" + ending
                )
                self.assertEqual(result["exit"], exit_code, result)
                self.assertEqual(
                    result["stdout"], "".join(f"{i}\n" for i in range(3000)) + "stdout tail"
                )
                self.assertEqual(result["stderr"], "stderr tail")
                self.assertFalse(result["stdout_truncated"])
                self.assertFalse(result["stderr_truncated"])
                self.assertEqual(
                    result["error"]["type"] if result["error"] else None, error, result
                )

    def test_timeout_retains_flushed_output_but_can_lose_buffered_tail(self):
        result = self.run_code(
            "import sys\n"
            "print('visible', flush=True)\n"
            "print('pending', end='')\n"
            "sys.stderr.write('visible\\n')\n"
            "sys.stderr.write('pending')\n"
            "while True: pass\n",
            "--timeout=1s",
        )
        self.assertEqual(result["error"]["type"], "TimeoutError", result)
        self.assertEqual(result["stdout"], "visible\n")
        self.assertEqual(result["stderr"], "visible\n")

    def test_cpython_stdlib_native_extension_and_args(self):
        result = self.run_code(
            """
            import hashlib, json, sys, sqlite3
            connection = sqlite3.connect(":memory:")
            print(json.dumps([sys.argv[1:], hashlib.sha256(b"x").hexdigest(),
                              connection.execute("select 42").fetchone()[0]]))
        """,
            args=["--flag", "two words"],
        )
        self.assertEqual(result["exit"], 0, result)
        output = json.loads(result["stdout"])
        self.assertEqual(output[0], ["--flag", "two words"])
        self.assertEqual(output[2], 42)

    def test_script_grants_its_own_directory_and_nothing_above_it(self):
        (self.script.parent / "sibling.txt").write_text("next to the script")
        result = self.run_code(f"print(open({str(self.script.parent / 'sibling.txt')!r}).read())")
        self.assertEqual(result["stdout"], "next to the script\n", result)
        self.assertEqual(result["policy"]["implicit_read"], [str(self.script.parent.resolve())])
        result = self.run_code(f"open({str(self.secret)!r}).read()")
        self.assert_denied(result)
        self.assertEqual(result["error"]["file"], str(self.script))
        self.assertEqual(result["error"]["line"], 1)
        result = self.run_code(f"open({str(self.script.parent / 'new.txt')!r}, 'w')")
        self.assert_denied(result)

    def test_script_arguments_cannot_grant_runtime_permissions(self):
        args = [
            f"--allow-read={self.secret}",
            "--allow-network",
            "--timeout=1m",
            "--optimize-json",
            "--max-output=1",
        ]
        result = self.run_code(
            f"import json, sys\nprint(json.dumps(sys.argv[1:]))\nopen({str(self.secret)!r}).read()",
            args=args,
        )
        self.assert_denied(result)
        self.assertEqual(json.loads(result["stdout"]), args)
        self.assertEqual(result["policy"]["allow_read"], [])
        self.assertEqual(result["policy"]["network"], "deny")
        self.assertEqual(result["policy"]["timeout_ms"], 5000)

    def test_allow_read_single_file(self):
        result = self.run_code(
            f"print(open({str(self.secret)!r}).read())",
            f"--allow-read={self.secret}",
        )
        self.assertEqual(result["stdout"], "private fixture\n", result)
        self.assertEqual(result["exit"], 0, result)

    def test_write_requires_permission_and_read_does_not_grant_it(self):
        for flags in [(), (f"--allow-read={self.root}",)]:
            result = self.run_code(f"open({str(self.secret)!r}, 'w').write('oops')", *flags)
            self.assert_denied(result)
            self.assertEqual(self.secret.read_text(), "private fixture")

    def test_write_permission_does_not_grant_read(self):
        output = self.root / "output"
        output.mkdir()
        code = f"open({str(output / 'result')!r}, 'w').write('ok')"
        result = self.run_code(code, f"--allow-write={output}")
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual((output / "result").read_text(), "ok")
        result = self.run_code(
            f"open({str(output / 'result')!r}).read()", f"--allow-write={output}"
        )
        self.assert_denied(result)

    def test_symlink_cannot_escape_allowed_directory(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        link = allowed / "link"
        link.symlink_to(self.secret)
        result = self.run_code(f"open({str(link)!r}).read()", f"--allow-read={allowed}")
        self.assert_denied(result)
        result = self.run_code(
            f"open({str(link)!r}, 'w').write('oops')", f"--allow-write={allowed}"
        )
        self.assert_denied(result)
        self.assertEqual(self.secret.read_text(), "private fixture")

    def test_native_open_and_truncate_are_denied(self):
        result = self.run_code(f"""
            import ctypes, errno, json
            libc = ctypes.CDLL(None, use_errno=True)
            opened = libc.open({os.fsencode(self.secret)!r}, 0)
            open_errno = ctypes.get_errno()
            truncated = libc.truncate({os.fsencode(self.secret)!r}, 0)
            print(json.dumps([opened, open_errno, truncated, ctypes.get_errno()]))
        """)
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual(json.loads(result["stdout"]), [-1, errno.EACCES, -1, errno.EACCES])
        self.assertEqual(self.secret.read_text(), "private fixture")

    def test_socket_denied_in_python_and_native_code(self):
        self.assert_denied(self.run_code("import socket; socket.socket()"))
        result = self.run_code("""
            import ctypes, json
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.socket(2, 1, 0)
            print(json.dumps([fd, ctypes.get_errno()]))
        """)
        self.assertEqual(json.loads(result["stdout"]), [-1, errno.EPERM])

    def test_asyncio_works_without_network_permission(self):
        result = self.run_code("""
            import asyncio
            async def main():
                values = await asyncio.gather(
                    asyncio.to_thread(lambda: 21), asyncio.to_thread(lambda: 21))
                print(sum(values))
            asyncio.run(main())
        """)
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual(result["stdout"], "42\n", result)

    def test_host_unix_sockets_denied_even_with_network_permission(self):
        result = self.run_code(
            "import socket; socket.socket(socket.AF_UNIX)",
            "--allow-network=true",
        )
        self.assert_denied(result)

    def test_datagram_socketpair_cannot_bypass_host_socket_policy(self):
        for flags in ((), ("--allow-network=true",)):
            result = self.run_code(
                "import socket; socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)",
                *flags,
            )
            self.assert_denied(result)

    def test_subprocess_and_exec_denied(self):
        self.assert_denied(
            self.run_code("import subprocess; subprocess.run(['/bin/true'], check=True)")
        )
        self.assert_denied(self.run_code("import os; os.execv('/bin/true', ['/bin/true'])"))

    def test_native_fork_denied(self):
        result = self.run_code("""
            import ctypes, json, os
            libc = ctypes.CDLL(None, use_errno=True)
            pid = libc.fork()
            if pid == 0:
                os._exit(90)
            print(json.dumps([pid, ctypes.get_errno()]))
        """)
        self.assertEqual(json.loads(result["stdout"]), [-1, errno.EPERM])

    def test_threads_still_work(self):
        result = self.run_code("""
            import threading
            thread = threading.Thread(target=lambda: print("thread ran"))
            thread.start()
            thread.join()
        """)
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual(result["stdout"], "thread ran\n")

    def test_cross_process_signals_are_denied(self):
        # Signal 0 only checks permission: this cannot terminate the supervisor
        # even if a filter regression occurs.
        result = self.run_code("""
            import ctypes, json, os
            libc = ctypes.CDLL(None, use_errno=True)
            killed = libc.kill(os.getppid(), 0)
            kill_errno = ctypes.get_errno()
            queued = libc.sigqueue(os.getppid(), 0, ctypes.c_void_p(0))
            print(json.dumps([killed, kill_errno, queued, ctypes.get_errno()]))
        """)
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual(json.loads(result["stdout"]), [-1, errno.EPERM, -1, errno.EPERM])

    def test_metadata_mutation_denied(self):
        original = self.secret.stat().st_mode
        result = self.run_code(f"import os; os.chmod({str(self.secret)!r}, 0o777)")
        self.assert_denied(result)
        self.assertEqual(self.secret.stat().st_mode, original)

    def test_environment_is_opt_in_and_pythonpath_is_ignored(self):
        injection = self.root / "sitecustomize.py"
        injection.write_text("print('INJECTED')")
        env = dict(os.environ, PYNOX_TEST_SECRET="fixture-value", PYTHONPATH=str(self.root))
        code = "import os; print(os.environ.get('PYNOX_TEST_SECRET', 'absent'))"
        result = self.run_code(code, env=env)
        self.assertEqual(result["stdout"], "absent\n", result)
        result = self.run_code(
            code,
            "--allow-env=PYNOX_TEST_SECRET",
            "--allow-env=PYTHONPATH",
            f"--allow-read={self.root}",
            env=env,
        )
        self.assertEqual(result["stdout"], "fixture-value\n", result)

    def test_loader_environment_cannot_be_forwarded(self):
        result = self.run_code("print('must not run')", "--allow-env=LD_PRELOAD")
        self.assertEqual(result["exit"], 125, result)
        self.assertEqual(result["stdout"], "")

    def test_inherited_descriptor_is_closed(self):
        with self.secret.open() as source:
            fd = source.fileno()
            result = self.run_code(
                f"""
                import os
                try:
                    print(os.read({fd}, 100).decode())
                except OSError:
                    print("closed")
            """,
                pass_fds=(fd,),
            )
        self.assertEqual(result["stdout"], "closed\n", result)

    def test_timeout_infinite_loop(self):
        result = self.run_code("while True: pass", "--timeout=150ms")
        self.assertEqual(result["exit"], 124, result)
        self.assertEqual(result["error"]["type"], "TimeoutError")
        self.assertLess(result["duration_ms"], 3000)

    def test_memory_limit(self):
        result = self.run_code("x = bytearray(512 * 1024 * 1024)", "--memory=64m")
        self.assertEqual(result["error"]["type"], "MemoryError", result)

    def test_output_is_bounded_and_json_is_not_mixed_with_script_output(self):
        result = self.run_code(
            """
            import os
            for _ in range(100):
                os.write(1, b"x" * 8192)
                os.write(2, b"y" * 8192)
        """,
            "--max-output=1000",
        )
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual(result["stdout"], "x" * 1000)
        self.assertEqual(result["stderr"], "y" * 1000)
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])

    def test_errors_use_script_line_and_syntax_location(self):
        result = self.run_code("from pathlib import Path\nPath('/etc/passwd').read_text()")
        self.assertEqual(result["error"]["file"], str(self.script), result)
        self.assertEqual(result["error"]["line"], 2)
        result = self.run_code("if True print('oops')")
        self.assertEqual(result["error"]["type"], "SyntaxError", result)
        self.assertEqual(result["error"]["line"], 1)

    def test_exit_codes_and_abrupt_exit(self):
        result = self.run_code("import sys; sys.exit(7)")
        self.assertEqual(result["exit"], 7, result)
        result = self.run_code("import os; os._exit(0)")
        self.assertEqual(result["exit"], 125, result)
        self.assertEqual(result["error"]["type"], "WorkerError")

    def test_package_directory_without_pth_execution(self):
        packages = self.root / "packages"
        packages.mkdir()
        (packages / "demo.py").write_text("value = 42")
        (packages / "evil.pth").write_text("import sys; print('INJECTED')")
        result = self.run_code("import demo; print(demo.value)", f"--package-dir={packages}")
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual(result["stdout"], "42\n")

    def test_fail_closed_when_landlock_is_unavailable(self):
        result = self.run_code("print('must not run')", preexec_fn=deny_landlock)
        self.assertEqual(result["exit"], 125, result)
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["error"]["type"], "SandboxSetupError")

    @unittest.skipUnless(platform.machine() == "x86_64", "x32 ABI only applies to x86_64")
    def test_x32_abi_is_rejected(self):
        result = self.run_code("""
            import ctypes
            ctypes.CDLL(None).syscall(0x40000000 + 39)
        """)
        self.assertEqual(result["error"]["type"], "SignalError", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
