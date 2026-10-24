# Architecture Decision Records

Pynox uses the numbered ADR format used by pydynox. Records capture context,
decision, alternatives and consequences. Accepted records describe v0.1 unless
explicitly marked as future work; an ambition is not an implemented feature.

| # | Decision | Status |
| --- | --- | --- |
| 001 | [An agent-first Python runtime on CPython](001-product-scope.md) | Accepted |
| 002 | [Rust integration and versioned CPython ABI](002-rust-and-cpython-abi.md) | Accepted |
| 003 | [Sandbox by default, with explicit permissions](003-sandbox.md) | Accepted; revised 2026-10-18 |
| 004 | [Transparent, measured native optimizations](004-native-optimizations.md) | Accepted |
| 005 | [Explicit pool and disposable task state](005-task-lifecycle.md) | Accepted |
| 006 | [Pinned, relocatable Linux distribution](006-distribution-and-dependencies.md) | Accepted |
| 007 | [Execution modes and structured diagnostics](007-execution-and-diagnostics.md) | Accepted; revised 2026-10-24 |
| 008 | [Validation and GitHub release workflow](008-validation-and-releases.md) | Accepted |
| 009 | [CPython 3.11–3.15 and pre-alpha maturity](009-python-matrix-and-pre-alpha.md) | Accepted |
| 010 | [Linux only: glibc and musl](010-linux-platforms.md) | Accepted |
| 011 | [Agent integration contract](011-agent-integration.md) | Accepted |
| 012 | [Configuration file for permissions, profiles and pool limits](012-configuration.md) | Accepted |

New records use `NNN-short-title.md` with Status, Context, Decision,
Alternatives considered and Consequences. Supersede decisions explicitly.

The set was first written on 2026-10-17 from the product discussion and the
preserved experiments, then revised the same day, before any release, when the
product direction was settled: agents as the primary workload, Linux only and
optimizations always on. On 2026-10-18 the sandbox became the default once
output was decoupled from the JSON result, and the configuration file was
added. Earlier wording, including the native macOS decision and the opt-in
sandbox, remains in git history and was never shipped.
