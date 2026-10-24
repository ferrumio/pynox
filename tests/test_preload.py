"""The same kernel and lifecycle contract also applies with a preloaded template."""

import json
import platform
import subprocess
import sysconfig
import tempfile
import unittest
from pathlib import Path

import test_pool as pool_tests
import test_runtime as runtime


class PreloadedRuntimeTests(pool_tests.PoolRuntimeTests):
    def start_pool(self, **kwargs):
        packages = self.root / "trusted"
        packages.mkdir(exist_ok=True)
        source = packages / "cached_package.py"
        if not source.exists():
            source.write_text(
                "import os\nIMPORT_PID = os.getpid()\n"
                "INITIAL_SECRET = os.environ.get('TASK_SECRET')\nVALUES = []\n"
            )
        super().start_pool(
            pool_flags=["--preload=cached_package", f"--package-dir={packages}"], **kwargs
        )

    def test_preload_occurs_once_and_state_is_private_to_each_child(self):
        result = self.run_code("""
            import cached_package, json, os
            cached_package.VALUES.append("private")
            print(json.dumps([cached_package.IMPORT_PID, os.getppid(), os.getpid()]))
        """)
        template, parent, first_child = json.loads(result["stdout"])
        self.assertEqual(template, parent)
        self.assertNotEqual(template, first_child)
        result = self.run_code("""
            import cached_package, json, os
            print(json.dumps([cached_package.IMPORT_PID, cached_package.VALUES, os.getpid()]))
        """)
        second_template, values, second_child = json.loads(result["stdout"])
        self.assertEqual(second_template, template)
        self.assertEqual(values, [])
        self.assertNotEqual(first_child, second_child)

    def test_task_environment_does_not_reach_template(self):
        result = self.run_code(
            "import cached_package, os; print(cached_package.INITIAL_SECRET, os.environ['TASK_SECRET'])",
            "--allow-env=TASK_SECRET",
            env={"TASK_SECRET": "only-this-child"},
        )
        self.assertEqual(result["stdout"], "None only-this-child\n", result)

    def test_job_cannot_request_preload(self):
        self.script.write_text("print('must not run')")
        result = self.request({"script": str(self.script), "preload": ["socket"]})
        self.assertEqual(result["error"]["type"], "RequestError", result)
        self.assertEqual(result["stdout"], "")

    def test_preloaded_packages_and_ancestors_cannot_be_writable(self):
        code = "print('must not run')"
        for path in (self.root / "trusted", self.root, self.root / "trusted/cached_package.py"):
            result = self.run_code(code, f"--allow-write={path}")
            self.assertEqual(result["error"]["type"], "RequestError", result)
            self.assertIn("preloaded runtime", result["error"]["message"])
            self.assertEqual(result["stdout"], "")

    def test_loaded_module_is_cached(self):
        (self.root / "trusted/cached_package.py").unlink()
        result = self.run_code("import cached_package; print(cached_package.VALUES)")
        self.assertEqual(result["stdout"], "[]\n", result)

    def test_preload_output_is_not_inherited_by_jobs(self):
        self.close_pool()
        (self.root / "trusted/cached_package.py").write_text(
            "import sys\n"
            "print('template stdout', end='')\n"
            "print('template stderr', end='', file=sys.stderr)\n"
        )
        self.start_pool()
        for _ in range(2):
            result = self.run_code("print('only the job')")
            self.assertEqual(result["exit"], 0, result)
            self.assertEqual(result["stdout"], "only the job\n")
            self.assertEqual(result["stderr"], "")

    def startup_failure(self, source, expected):
        modules = self.root / "invalid"
        modules.mkdir(exist_ok=True)
        (modules / "invalid_preload.py").write_text(source)
        completed = subprocess.run(
            [
                str(runtime.BINARY),
                "pool",
                "--workers=1",
                "--preload=invalid_preload",
                f"--package-dir={modules}",
            ],
            input="",
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 125, completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(result["event"], "error")
        self.assertIn(expected, result["message"])

    def test_preload_with_open_file_is_rejected(self):
        self.startup_failure("handle = open(__file__)\n", "file descriptor")

    def test_preload_with_thread_is_rejected(self):
        self.startup_failure(
            "import threading, time\nthreading.Thread(target=lambda: time.sleep(60), daemon=True).start()\n",
            "native threads",
        )


PACKAGES = Path(sysconfig.get_path("purelib"))


@unittest.skipUnless(
    (PACKAGES / "numpy").is_dir() and (PACKAGES / "pandas").is_dir(),
    "Optional NumPy/pandas are not installed in the development environment",
)
@unittest.skipUnless(platform.system() == "Linux", "Linux sandbox backend")
class NativePreloadTests(unittest.TestCase):
    def test_native_computation_threads_permissions_and_fresh_state(self):
        with tempfile.TemporaryDirectory(prefix="pynox-native-preload-") as temporary:
            root = Path(temporary)
            scripts = [
                """import json, threading
import numpy as np
import pandas as pd
values = []
thread = threading.Thread(target=lambda: values.append(int((np.ones((32, 32)) @ np.ones((32, 32))).sum())))
thread.start()
thread.join()
groups = pd.DataFrame({"group": [1, 1, 2], "value": [2, 3, 4]}).groupby("group")["value"].sum().tolist()
np._pynox_private = "first task"
print(json.dumps([values, groups]))
""",
                """import json, ctypes, socket, numpy as np
libc = ctypes.CDLL(None, use_errno=True)
fd = libc.open(b"/etc/passwd", 0)
file_errno = ctypes.get_errno()
try:
    socket.socket()
    socket_denied = False
except PermissionError:
    socket_denied = True
print(json.dumps([hasattr(np, "_pynox_private"), fd, file_errno, socket_denied]))
""",
            ]
            requests = []
            for index, code in enumerate(scripts):
                script = root / f"{index}.py"
                script.write_text(code)
                requests.append({"id": index, "script": str(script), "memory": "1g"})
            completed = subprocess.run(
                [
                    str(runtime.BINARY),
                    "pool",
                    "--workers=1",
                    "--preload=numpy,pandas",
                    f"--package-dir={PACKAGES}",
                ],
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(responses[0]["event"], "ready")
            self.assertTrue(all(result["exit"] == 0 for result in responses[1:]), responses)
            self.assertEqual(json.loads(responses[1]["stdout"]), [[32768], [5, 4]])
            self.assertEqual(json.loads(responses[2]["stdout"]), [False, -1, 13, True])
