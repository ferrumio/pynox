# Seccomp allowlist: paired measurements

Measured on 2026-10-18, Linux aarch64/glibc, CPython 3.11.17, CPU affinity
`[0, 1]`. The baseline is commit `2aef700`; the candidate changes only
`src/sandbox.rs` in production code. Raw reports record source and binary hashes.
No historical benchmark was replaced.

Run on each build:

```bash
make build
uv run --locked python scripts/benchmark.py
```

The script runs five workloads in five modes: seven shuffled rounds plus one
discarded warmup per mode. Each report contains 175 timed observations with
validated output. Filesystem cache is warm; writes do not use `fsync`. Memory
is not measured. p95 is nearest rank (the maximum with seven observations).
These are before/after runs on one host, not a cross-platform performance claim.

## Minimal-script startup / request latency

All times are milliseconds, measured by the client. `pynox` and `json` include
process startup, sandbox setup and execution; `pool` is a request to an already
started pool, including a fresh sandboxed child. CPython and `no_sandbox` are
controls whose execution paths did not change.

| Mode | Before p50 | After p50 | Delta p50 | Before p95 | After p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `cpython` | 7.564 | 7.769 | +0.205 | 7.724 | 7.928 |
| `no_sandbox` | 8.051 | 8.098 | +0.046 | 8.138 | 8.365 |
| `pynox` | 10.557 | 11.033 | +0.476 | 11.366 | 11.089 |
| `json` | 10.575 | 10.985 | +0.409 | 10.653 | 11.128 |
| `pool` | 4.588 | 4.922 | +0.334 | 4.621 | 5.027 |

The default sandboxed command increased by **0.476 ms p50** in this sample,
below the task's 0.5 ms justification threshold but close to it. This is a
measurable cost, not a performance improvement. The explicit allowlist has more
BPF rules to build, verify and evaluate than the old denylist; this sample alone
does not isolate that cost from host noise (the controls also moved). The policy
change is justified by denying previously unlisted and future native syscalls.

Full workload timings, p95, inputs, build flags and hashes:

- [Before: denylist](before.json)
- [After: allowlist](after.json)
