# Changelog

## 0.1.0 — unreleased

- Introduce Pynox as a pre-alpha, agent-first Python runtime: a Rust launcher
  embedding CPython 3.11–3.15, with 3.11 as the default and per-minor pins.
- Sandbox `pynox script.py`, `-c` and `-m` by default with Python's own
  stdin, stdout, stderr and exit status; `--json` returns one structured
  result and `--no-sandbox` runs CPython's unrestricted command line.
- Read `pynox.toml` for standing permissions, named profiles and pool
  settings, including `[pool.limits]` as a ceiling for every request.
- Enforce filesystem, network and process restrictions, bounded output,
  environment grants, wall timeout and address-space limits per task. The
  script's directory is the only implicit read grant.
- Support asyncio's internal wakeup socketpair and opt-in IPv4/IPv6.
- Replace the seccomp denylist with an explicit allowlist adapted from Moby
  v28.0.4. Unknown native syscalls return `EPERM`; retain thread-only `clone`,
  the `clone3` `ENOSYS` fallback and fatal x32 ABI rejection. Validate generated
  BPF without applying it to the Rust test runner.
- Retain a JSONL pool with clean templates and disposable per-task children;
  accept inline `code` with an optional `name` instead of a script file.
- Ship the standard library precompiled in every bundle, with unchecked-hash
  bytecode that survives archive time normalization: common imports from a
  read-only bundle drop from 268 ms to 56 ms.
- Add an AWS Lambda layer (`scripts/lambda_layer.py`, built by CI for glibc
  bundles): a `provided.al2023` custom runtime whose Runtime API loop uses a
  plain socket, measured at about 200 ms init; and multi-architecture
  container images per CPython minor and libc, built from the bundles with
  no build step and pushed to GHCR by the release workflow.
- Govern the repository: rulesets for `main` (pull requests, a single
  `ci-ok` check, no force pushes) and for `v*` tags, `CODEOWNERS`, CodeQL,
  the OpenSSF Scorecard, and a `GOVERNANCE.md`.
- Add real agent examples in `examples/agents/`: a data analyst and a coding
  agent on Pydantic AI and a support agent on Strands Agents, calling Claude
  on Bedrock, with a docs page of real transcripts and CI tests that use
  scripted models. The framework dependencies live in a new `agents` group.
- Put the permissions and host function signatures before `Args:` in the
  docstring `pynox.tool()` generates, because Strands and other
  Google-style readers drop everything after it; resolve string annotations.
- Add four agent use cases (data analyst, support, coding, API) as runnable
  examples tested on every commit, with a use cases page built from their
  real output.
- Add host functions: a pool request declares `functions`, the code calls
  them as `pynox_host.NAME(...)`, the pool emits a `call` event and the
  client's `reply` returns the value; undeclared names never reach the
  client. `Pool.run(functions=...)` and `pynox.tool(functions=...)` handle
  the protocol and describe the functions to the model.
- Add `scripts/benchmark_pool.py` and record pool throughput, latency and
  memory for 1 to 8 workers; throughput scales linearly to about 1,570 small
  jobs per second with 8 workers on the reference host.
- Add pool observability: `{"stats": true}` returns counters by outcome and
  error type, `--log=json` (or `[pool] log`) writes one JSON line per job to
  stderr without captured output, and `Pool.stats()` exposes the counters.
- Update the development dependency pytest to 9.0.3 for its tmpdir advisory.
- Add `max_file_size` (flag, configuration, pool request, package) backed by
  `RLIMIT_FSIZE`, with `SIGXFSZ` ignored so oversized writes raise `OSError`;
  a `[pool.limits]` value caps requests and is their default.
- Add persistent sessions to the pool: `"session": NAME` keeps one isolated
  process whose `__main__` survives between requests, with permissions fixed
  by the first request, `close`, idle timeout and a session limit;
  `pynox.tool(session=True)` and `Pool.session()` expose it.
- Ship the `pynox` Python package: `pynox.run`, `pynox.tool` and `Pool` over
  the pool protocol, with a per-CPython-minor wheel (`make wheel`) that embeds
  the binary and a matching CPython and installs a `pynox` console script.
- Add tested agent integration examples on that package: a tool for any
  framework and a Monty bridge where Monty orchestrates and Pynox executes.
- Apply a bounded `json.dump` fast path transparently, installed lazily when the
  application imports `json`; `--optimize-json` and the pool's `optimize_json`
  field no longer exist.
- Keep `pynox doctor` as the capability probe: it reports runtime and sandbox
  support separately and lists why the sandbox cannot run (Landlock, `prctl`,
  `pidfd_open`), as measured on AWS Lambda.
- Report Pynox and the loaded CPython runtime together in `--version` / `-V`.
- Keep flags after the script in Python's arguments, preventing them from
  changing runtime permissions or limits.
- Add Rust unit tests under `tests/rust/` and run them alongside Python
  integration tests in `make test` and the CI matrix.
- Separate Rust sources in `src/` from embedded Python in `python/pynox/`,
  following pydynox's layout.
- Add relocatable CPython bundles, checksum installation, ADRs and GitHub CI.
- Build/test five Python versions on two Linux architectures and publish separate
  ABI bundles as draft prereleases; allow installed Python variants to coexist.
- Target Linux only: glibc and musl on x86_64/ARM64, built in pinned
  manylinux/musllinux images with a glibc symbol audit. The macOS build drafted
  earlier was removed before release.
- Cut launcher overhead: precompile embedded Python at build time, pass task
  data to the worker as objects instead of importing `json`, and wait for the
  worker on a pidfd instead of a 5 ms polling sleep.
- Run sandboxed tasks in UTF-8 mode; the isolated configuration previously
  left `open()`, stdout and the filesystem encoding in ASCII.
- Measure the pool's request/response latency in `scripts/benchmark.py`.
- Preserve the earlier research outside the active source tree.
