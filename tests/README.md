# Testing Pynox

Run both suites with `make test`, or only Rust unit tests with `make test-rust`.
The build helper selects the matching CPython and runs
`cargo test --locked --all-targets`.

```bash
make test-rust PYNOX_PYTHON_VERSION=3.15
make test PYNOX_PYTHON_VERSION=3.15
```

The default is Python 3.11. GitHub CI covers five pinned CPython versions on
glibc and musl Linux, each on x86_64 and aarch64.

## Rust unit tests

All Rust test bodies live in `tests/rust/`. Implementation modules reference
these files with `#[cfg(test)]` and `#[path]` so the tests can check private
logic without making it public or entering release builds.

| File | Coverage |
| --- | --- |
| [policy.rs](rust/policy.rs) | Limits, overflow, argument boundaries, environment names and permission validation |
| [pool.rs](rust/pool.rs) | Request validation, defaults, environment separation and bounded JSONL framing |
| [capture.rs](rust/capture.rs) | Nonblocking pipes, byte limits, draining and descriptor flags |
| [diagnostics.rs](rust/diagnostics.rs) | Linux supervision: output capture, interrupted reads, timeout/signal precedence and structured results |
| [sandbox.rs](rust/sandbox.rs) | Generated seccomp BPF decisions: allowlist/default errno, network, threads, resource queries and ABI compatibility; no filter installed in the test process |

These tests do not initialize CPython or apply a sandbox to the test runner.
They exercise Rust logic and real pipes; kernel enforcement is checked in
separate processes by the integration suite.

## Python integration tests

The Python tests launch the compiled Rust executable and inspect its behavior.
They cover kernel permissions, native calls, CPython compatibility, framework
execution and pool lifecycle, including isolation between jobs.
[test_agents.py](test_agents.py) runs the agent examples: the pool tool and,
when `pydantic-monty` is installed, the Monty bridge.

With a matching binary already built, run them directly:

```bash
uv run --locked --group integration pytest
```

Keep the test driver's Python version aligned with the embedded runtime when
testing native packages. `make test PYNOX_PYTHON_VERSION=...` selects both.
See [CONTRIBUTING.md](../CONTRIBUTING.md) for prerequisites and packaging checks.
