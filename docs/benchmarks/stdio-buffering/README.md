# Stdio buffering experiment — 2026-10-24

Issue #6: restore CPython buffering in sandboxed tasks and batch the
supervisor's JSON writes. Baseline: `b06d42c3d41833c90e616d0d40a263eb6664003b`.
These are new measurements; earlier benchmark artifacts are unchanged.

## Method

Linux ARM64, glibc 2.34, kernel 6.18.48, CPython 3.11.17, affinity CPUs 0–1.
The stock executable and Pynox use the same CPython distribution; stock
CPython links its interpreter statically and Pynox embeds shared libpython.

`uv run --locked python scripts/benchmark.py` was run before and after the
runtime changes, using the same updated benchmark script. Seven shuffled
rounds per workload/mode follow one discarded warmup; the filesystem is
warm. Timing includes process startup, execution, output transfer and exit.
Pool timing includes request serialization and response parsing, but excludes
template startup. Cold `--json` timing excludes the client's outer JSON parse.
All output is checked outside the timer, except the pool's response parse.

The new workload is `for i in range(200000): print(i)`: 1,288,890 stdout bytes.
Captured modes explicitly raise `max_output` to retain **all** those bytes.
Exact output and absence of truncation are asserted. These numbers therefore
include capture, JSON serialization and transfer, not just execution.

- [before.json](before.json): baseline runtime.
- [python-buffering-only.json](python-buffering-only.json): intermediate
  experiment, before batching the Rust JSON writer.
- [after.json](after.json): final runtime.
- [stdio-detail.json](stdio-detail.json): separate paired measurements with
  both default and full JSON capture, GNU time RSS samples and strace counts.

The main reports include raw observations, interpreter build flags, binary
and source hashes. Reproduce by building the baseline and changed trees with
the same CPython, copying the new `scripts/benchmark.py` into the baseline
tree, and running the command above separately in each tree. Preserve each
`dist/benchmark.json` before the next run.

## Results

End-to-end median, milliseconds:

| Mode | 200k prints before | After | Improvement |
| --- | ---: | ---: | ---: |
| CPython | 55.99 | 56.46 | control |
| `--no-sandbox` | 59.10 | 59.69 | control |
| `pynox` | 243.02 | 61.65 | 3.94× |
| `--json`, full capture | 428.38 | 64.84 | 6.61× |
| Pool, full capture | 263.36 | 57.48 | 4.58× |

The final default mode is 9.2% above CPython. **Full JSON capture remains
14.9% above CPython**, so this experiment does not establish the issue's
within-10% target for an untruncated result. It does remove the excessive
per-print writes. The pool is within 1.8% of CPython while returning all output.

With only Python buffering fixed, JSON took 68.39 ms and the pool 67.70 ms.
Batching the serializer's small writes helps both without allocating another
full serialized result.

Tiny-script startup:

| Mode | Before | After | Difference |
| --- | ---: | ---: | ---: |
| CPython | 7.324 | 7.475 | +0.151 ms |
| `pynox` | 10.075 | 10.234 | +0.159 ms |
| `--json` | 10.164 | 10.328 | +0.163 ms |
| Pool | 4.332 | 4.570 | +0.238 ms |

No measured startup increase exceeds 0.5 ms. The controls also shifted;
these are single-host observations, not significance estimates or promises
for every platform. Other workloads and all samples remain in the raw reports.

## Default capture limit and syscall counts

A separate 15-round shuffled comparison used the same before/after binaries,
affinity and output validation. With the default JSON capture limit:

| Command | Before | After | After vs CPython |
| --- | ---: | ---: | ---: |
| CPython | 56.40 ms | 56.40 ms | control |
| `pynox prints.py` | 234.52 ms | 61.41 ms | +8.9% |
| `pynox --json prints.py` | 405.11 ms | 59.11 ms | +4.8% |
| `pynox --json --max-output=1288890 prints.py` | 424.40 ms | 64.47 ms | +14.3% |

**The default JSON command truncates at 65,536 bytes**, and the benchmark
asserts the exact prefix and `stdout_truncated=true`. It meets the issue's
10% target for that command, but must not be presented as full-output
performance. The full-capture row independently confirms the remaining gap.

`strace -f -c -e trace=write` counted writes by the command and its descendants:

| Command | Before | After |
| --- | ---: | ---: |
| CPython | 158 | 158 |
| `pynox` | 400,002 | 160 |
| `--json`, full capture | 401,458 | 342 |

Only these diagnostic runs used `--timeout=60s` to accommodate ptrace overhead;
their elapsed times are not benchmark measurements. The supplemental raw
report contains all 15 untraced observations per command and the trace summaries.

## Compatibility and memory

Stdout follows CPython's pipe/file versus terminal buffering, stderr stays
line-buffered, and terminal prompts are tested with a PTY. Completion,
exceptions, `sys.exit` and session-step boundaries flush pending output.
Trusted preload output is flushed to `/dev/null` before the template forks.
The serializer uses a bounded `BufWriter`, retaining the same JSON bytes
and flushing each response under the output lock.

A timeout or SIGKILL can discard the pending tail; explicitly flushed output
is retained subject to capture limits. `-u` remains a sandbox no-op. These
changes neither widen sandbox permissions nor change the pool protocol.

CPython's normal stream buffers and the Rust writer add bounded buffering.
RSS samples in the detail report use three separate `/usr/bin/time -f %M`
runs per mode, with stdout drained through a pipe. This is the maximum RSS
of an individual process reported by GNU time, **not aggregate process-tree
memory**, and does not measure concurrent pool memory.
Median maximum RSS was 23,056 KiB before and 22,940 KiB after for each Pynox
command, versus 12,392 KiB for CPython. No peak RSS increase was observed;
the small decrease is not treated as a memory optimization.
