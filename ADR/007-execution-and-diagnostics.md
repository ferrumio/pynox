# ADR 007: Execution modes and structured diagnostics

## Status

Accepted — 2026-10-17. Revised 2026-10-18 with passthrough output and
2026-10-24 with CPython stdio buffering.

## Context

Ordinary tooling expects stdout/stderr, command-line switches and exit codes.
Agents need bounded machine-readable results with error locations and the
effective policy. Earlier revisions tied the JSON result to the sandbox, which
forced a choice between a usable terminal tool and an isolated one.

## Decision

Output format and isolation are independent. By default the sandboxed task's
stdin, stdout and stderr are the user's; exceptions print Python's traceback,
followed by `pynox: <hint>` when a permission or memory hint applies; launcher,
timeout and signal failures print one `pynox:` line. Exit codes are Python's,
plus 124 for timeout, 125 for launcher or setup failure and 2 for invalid
Pynox flags.

Standard streams use CPython's default buffering: stdout is block-buffered
for a pipe or file and line-buffered for a terminal; stderr is line-buffered.
After a pool fork, buffering follows the child's remapped descriptors.
Trusted preload output is flushed to the template's null device before
forking, so jobs cannot inherit pending output. Both streams are flushed
before a task or session-step report, including exceptions and `sys.exit`;
normal interpreter finalization still runs `atexit` handlers.
The supervisor batches JSON serialization writes through a bounded buffer
and flushes every response while holding the output lock. JSON bytes and
pool framing stay unchanged; there is no extra full-result allocation.

`--json` captures both streams, bounded by `--max-output`, and prints one JSON
result: `exit`, `stdout`, `stderr`, truncation flags, duration, runtime and the
effective policy including implicit grants, and an `error` with type, message,
file, line and hint. The pool returns the same shape plus request IDs.
`--no-sandbox` hands the command line to CPython unchanged. Pynox's own flags
are long options CPython does not use, so they can precede Python's.

In the sandbox the program is a script, `-c` or `-m`; isolation and buffering
options are accepted as no-ops, other interpreter options are refused with a
pointer to `--no-sandbox`. `pynox doctor` reports runtime metadata, probes
Landlock, `prctl` and `pidfd_open` without changing its own process, and
returns `sandbox_supported` with `sandbox_unavailable_reasons`, separate from
`runtime_supported`, so a blocked environment is diagnosed before a task fails.

The pool reports on itself as well as on tasks: a `stats` request returns
counters by outcome and error type, and `--log=json` emits one stderr line per
job with identifiers, outcome and timing. Neither carries captured output,
which may hold secrets; logging is off by default so embedding applications
do not inherit noise on their stderr.

## Alternatives considered

- JSON on every invocation: unusable as a terminal tool.
- Tracebacks only: a model cannot act on them reliably.
- Support every CPython option inside the sandbox: each needs its own
  isolated-configuration mapping; the three program forms cover scripts,
  tests and tools.

## Consequences

Passthrough tracebacks omit the launcher's frame but otherwise match Python.
Partial output before a `RecursionError` differs by that frame. The diagnostic
channel is written by the task and is not an audit log.

Batching stdout writes removes the syscall-per-print cost for captured and
redirected output. Terminal lines and `input()` prompts remain immediate.
A timeout or other `SIGKILL` can discard the last unflushed bytes, just as
with CPython. Use `print(..., flush=True)` or `stream.flush()` when output
must leave Python's buffer before termination. Capture limits still apply;
the JSON/pool result format and one-report-per-step boundary are unchanged.
