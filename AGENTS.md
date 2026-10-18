# Working on Pynox

Read README.md and ADR/README.md first. Pynox is an agent-first CPython
runtime: a Rust launcher, a kernel sandbox on by default, a warm pool and measured
native optimizations, in one Linux binary that also works as `python`.

- Preserve ordinary Python source/imports. Do not introduce mandatory frameworks.
- `pynox script.py` is sandboxed by default with Python's normal terminal
  behavior; `--json` returns the structured result and `--no-sandbox` is plain
  CPython. The only implicit grant is the script's directory (cwd for `-m`).
- `pynox.toml` is operator configuration: discovered from `--config`,
  `PYNOX_CONFIG` or the current directory, never beside the script.
- Optimizations are always on and lazy, never behind a flag. Each needs an
  eligibility contract, a fallback to CPython's behavior and paired measurements.
- Startup is a budget: embedded Python is precompiled in `build.rs`, and the
  bootstrap must not import stdlib modules beyond what CPython already loads.
- Kernel policy must fail closed; Python monkeypatching is not a sandbox.
- Treat runtime ABI and Python behavior as separate compatibility contracts.
- Keep native object/reference access safe under the supported GIL model.
- Optimize measured costs; compare equal lifecycles and record regressions/memory.
- Pool reuse is explicit. Never reuse task globals or fork the threaded supervisor.
- Linux only (glibc and musl, x86_64 and aarch64). Do not add platform guards
  or macOS paths without a new ADR.
- `examples/agents/` holds real framework agents (Pydantic AI, Strands); their
  dependencies are the `agents` group, glibc only. `tests/test_agent_examples.py`
  drives them with scripted models; never call a real model from tests.
- Agent integrations are product: `examples/agent_tool.py` and
  `examples/monty_bridge.py` run in the test suite; the pool protocol is a
  public contract and changes need an ADR.
- Keep Cargo.lock, uv.lock, version metadata, docs and ADRs consistent.
- Deployment assets live in `deploy/` (Dockerfile, Lambda bootstrap and
  runtime); they are built by `scripts/image_context.py` and
  `scripts/lambda_layer.py` and tested in CI.
- Keep Rust implementation in `src/`, the `pynox` Python package in
  `python/pynox/` and embedded runtime Python in `python/pynox/_embedded/`,
  following pydynox's source layout. Development tooling belongs in `scripts/`.
- Keep Rust tests in `tests/rust/`; implementation modules only reference them
  with `#[cfg(test)]` and `#[path]`, without inline test bodies.
- Use `make build`, `make check`, `uv run --locked --group integration pytest`,
  `make docs`, `make bundle`, and `uv run --locked python scripts/check_install.py`
  as appropriate to the change.
- Do not relabel historical research as results for a changed binary.
- CI files follow pydynox's pinned-action and draft-release conventions, adapted
  for executable bundles. Distinguish local validation from remote CI results.
