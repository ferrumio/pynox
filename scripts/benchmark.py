"""Paired end-to-end measurements; informational, never a shared-runner speed gate."""

import hashlib
import json
import math
import os
import platform
import random
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/release/pynox"
MODES = ("cpython", "no_sandbox", "pynox", "json", "pool")


class Pool:
    """One persistent `pynox pool` process; each request is a sandboxed job."""

    def __init__(self):
        self.process = subprocess.Popen(
            [str(BINARY), "pool", "--workers=1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        ready = json.loads(self.process.stdout.readline())
        assert ready["event"] == "ready", ready
        self.sequence = 0

    def run(self, script, root, max_output):
        self.sequence += 1
        request = {
            "id": self.sequence,
            "script": str(script),
            "allow_read": [str(root)],
            "allow_write": [str(root)],
            "max_output": max_output,
        }
        start = time.perf_counter_ns()
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        report = json.loads(self.process.stdout.readline())
        duration = (time.perf_counter_ns() - start) / 1_000_000
        assert report["id"] == self.sequence, report
        return report, duration

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


def main():
    info = json.loads(subprocess.check_output([BINARY, "doctor"]))
    python = info["python"]["executable"]
    build = json.loads(
        subprocess.check_output(
            [
                python,
                "-I",
                "-S",
                "-c",
                "import json,sysconfig; print(json.dumps({k:sysconfig.get_config_var(k) "
                "for k in ['CONFIG_ARGS','CC','CFLAGS','Py_GIL_DISABLED']}))",
            ]
        )
    )
    affinity = sorted(os.sched_getaffinity(0))[:2]
    os.sched_setaffinity(0, affinity)
    cases = []
    rng = random.Random(1907)
    with tempfile.TemporaryDirectory(prefix="pynox-benchmark-") as temporary:
        root = Path(temporary)
        workloads = {
            "empty": ("print('ok')\n", "ok\n"),
            "print_200000": (
                "for i in range(200000): print(i)\n",
                "".join(f"{i}\n" for i in range(200000)),
            ),
            "python_cpu": (
                "print(sum(i*i for i in range(200000)))\n",
                f"{sum(i * i for i in range(200000))}\n",
            ),
            "asyncio": (
                "import asyncio\nasync def main():\n"
                "    print(sum(await asyncio.gather(*"
                "[asyncio.to_thread(lambda: 7) for _ in range(32)])))\n"
                "asyncio.run(main())\n",
                "224\n",
            ),
        }
        for count in (32, 32768):
            rows = [
                {
                    "id": i,
                    "description": "ordinary Python data " * 3,
                    "price": i % 101,
                    "quantity": i % 7 + 1,
                }
                for i in range(count)
            ]
            source = root / f"input-{count}.json"
            source.write_text(json.dumps(rows))
            for row in rows:
                row["total"] = row["price"] * row["quantity"]
            expected = json.dumps(rows).encode()
            workloads[f"json_{count}"] = (
                "import json\n"
                f"with open({str(source)!r}, encoding='utf-8') as f: rows=json.load(f)\n"
                "for row in rows: row['total']=row['price']*row['quantity']\n"
                f"with open({str(root / 'output.json')!r}, 'w', encoding='utf-8') as f:\n"
                "    json.dump(rows,f)\n"
                "print(sum(row['total'] for row in rows))\n",
                f"{sum(row['total'] for row in rows)}\n",
                hashlib.sha256(expected).hexdigest(),
                source.stat().st_size,
            )
        pool = Pool()
        try:
            for workload, details in workloads.items():
                script = root / f"workload_{workload}.py"
                script.write_text(details[0])
                max_output = max(65536, len(details[1].encode()))
                for repetition in range(-1, 7):  # one discarded warmup per mode
                    modes = list(MODES)
                    rng.shuffle(modes)
                    for mode in modes:
                        if mode == "pool":
                            report, duration = pool.run(script, root, max_output)
                            assert report["exit"] == 0, report
                            assert not report["stdout_truncated"], (workload, mode)
                            output = report["stdout"]
                        else:
                            if mode == "cpython":
                                command = [python, str(script)]
                            elif mode == "no_sandbox":
                                command = [str(BINARY), "--no-sandbox", str(script)]
                            else:
                                command = [str(BINARY)]
                                if mode == "json":
                                    command += ["--json", f"--max-output={max_output}"]
                                command += [
                                    f"--allow-read={root}",
                                    f"--allow-write={root}",
                                    str(script),
                                ]
                            start = time.perf_counter_ns()
                            result = subprocess.run(
                                command, text=True, capture_output=True, timeout=15
                            )
                            duration = (time.perf_counter_ns() - start) / 1_000_000
                            assert result.returncode == 0 and not result.stderr, result
                            output = result.stdout
                            if mode == "json":
                                report = json.loads(output)
                                assert report["exit"] == 0, report
                                assert not report["stdout_truncated"], (workload, mode)
                                output = report["stdout"]
                        assert output == details[1], (workload, mode, output)
                        if workload.startswith("json"):
                            digest = hashlib.sha256((root / "output.json").read_bytes())
                            assert digest.hexdigest() == details[2], (workload, mode)
                        if repetition >= 0:
                            cases.append(
                                {
                                    "workload": workload,
                                    "mode": mode,
                                    "round": repetition,
                                    "milliseconds": duration,
                                }
                            )
        finally:
            pool.close()
        summary = []
        for workload in workloads:
            baseline = statistics.median(
                row["milliseconds"]
                for row in cases
                if row["workload"] == workload and row["mode"] == "cpython"
            )
            for mode in MODES:
                times = sorted(
                    row["milliseconds"]
                    for row in cases
                    if row["workload"] == workload and row["mode"] == mode
                )
                median = statistics.median(times)
                summary.append(
                    {
                        "workload": workload,
                        "mode": mode,
                        "p50_ms": median,
                        "p95_ms": times[math.ceil(len(times) * 0.95) - 1],
                        "speedup_vs_cpython": baseline / median,
                    }
                )
        report = {
            "runtime": info,
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "python_build": build,
            "modes": {
                "cpython": "Stock CPython executable from the same distribution (static libpython)",
                "no_sandbox": "`pynox --no-sandbox script.py`: embedded shared libpython, "
                "transparent optimizations, no supervisor",
                "pynox": "`pynox script.py`: the default; supervised sandboxed worker, "
                "Landlock and seccomp, output passed through",
                "json": "`pynox --json script.py`: as above with captured output and a JSON result",
                "pool": "`pynox pool --workers=1`: request/response latency of a forked child "
                "from a warm template, measured by the client",
            },
            "platform": platform.platform(),
            "affinity": affinity,
            "method": "Seven shuffled rounds, one discarded warmup; warm filesystem; no fsync",
            "p95_method": "Nearest rank; with seven observations p95 is the maximum",
            "memory": "Not measured in this run; see historical research for prior observations",
            "input_bytes": {
                name: data[3] for name, data in workloads.items() if name.startswith("json")
            },
            "expected_stdout_bytes": {
                name: len(data[1].encode()) for name, data in workloads.items()
            },
            "sources": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [
                    Path(__file__),
                    BINARY,
                    *sorted((ROOT / "src").rglob("*.rs")),
                    *sorted((ROOT / "python").rglob("*.py")),
                ]
                if p.is_file()
            },
            "cases": cases,
            "summary": summary,
        }
    (ROOT / "dist").mkdir(exist_ok=True)
    destination = ROOT / "dist/benchmark.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    for row in summary:
        print(
            f"{row['workload']:14} {row['mode']:10} "
            f"{row['p50_ms']:8.2f} ms  {row['speedup_vs_cpython']:.2f}x"
        )
    print(destination)


if __name__ == "__main__":
    main()
