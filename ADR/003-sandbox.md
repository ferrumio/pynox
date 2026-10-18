# ADR 003: Sandbox by default, with explicit permissions

## Status

Accepted — 2026-10-17. Revised 2026-10-18: the sandbox became the default
again once its output was decoupled from the JSON result (ADR 007).
Revised again on 2026-10-18: replace seccomp's syscall denylist with an
explicit allowlist, retaining the tested network, thread and ABI contracts.

## Context

Controlled execution is the product's proof point for agents. Python-level
replacement of `open` cannot enforce policy for native extensions or ctypes.
The first design made the sandbox the only way to run a file and printed a
JSON result, so `pynox hello.py` did not print `Hello`; the second made plain
Python the default and hid the sandbox behind a subcommand. Both were the
wrong trade: the output format was coupled to isolation. Deno showed that a
runtime can be secure by default and still feel like the ordinary tool when
permissions are explicit and the terminal behaves normally.

## Decision

`pynox script.py`, `pynox -c CODE` and `pynox -m MODULE` run inside the
sandbox with Python's own stdout, stderr, stdin and exit status. Permissions
are deny by default and granted explicitly by flags or by `pynox.toml`
(ADR 012). One implicit grant exists: the script's directory is readable, so
sibling imports work; `-m` makes the current directory readable instead,
mirroring `sys.path[0]`. `-c` grants nothing. `--no-sandbox` is the explicit
way to run CPython's unrestricted command line, including interpreter options
the sandbox does not support.

The backend is Landlock ABI >= 3, seccomp and resource limits; unsupported
or partial setup fails closed with a message pointing at `--no-sandbox`, and
never falls back silently. Allow unnamed AF_UNIX socketpairs for asyncio while
denying host Unix sockets; network stays denied unless granted and never
enables subprocesses. Python and native extensions share the kernel policy.
Cold execution applies policy before CPython initialization; pool templates
hold only trusted setup. Workers run in UTF-8 mode like stock CPython under
`C.UTF-8`.

Seccomp permits an explicit set of native x86_64/aarch64 syscalls and returns
`EPERM` for unlisted calls or arguments. The source is the public Moby default
profile at **v28.0.4**, commit
`6430e49a55babd9b8f4d08e70ecb2b68900770fe`
([profile](https://github.com/moby/moby/blob/6430e49a55babd9b8f4d08e70ecb2b68900770fe/profiles/seccomp/default.json)),
adapted rather than copied wholesale: retain file I/O, memory, signal handling,
clocks, libc thread support and event loops. Omit container-only allowances for
processes, host IPC, identity/capability changes and kernel administration.
Landlock continues to mediate allowed file operations.

The network grant adds IPv4/IPv6 socket creation and connection operations.
Without it, only unnamed Unix stream socketpairs can be created, including
their close-on-exec/nonblocking variants. Existing descriptor I/O remains
available for asyncio. `clone` requires `CLONE_THREAD`; `prlimit64` can only
query the current process. `prctl` is limited to thread names and selected
read-only queries.

Two existing exceptions remain explicit: `clone3` returns `ENOSYS`, as in
Moby, so libc can fall back to thread-only `clone`; an incompatible audit
architecture or x32 ABI retains the existing fatal rejection. Thus `EPERM`
is the default for unknown **native** syscalls, not a change to the tested
x32 `SignalError` contract. Install the compatibility/ABI filters before the
final allowlist; the final layer does not grant further filter installation.
No task runs if any layer fails.

## Alternatives considered

- Plain Python by default with an opt-in subcommand: compatible, but the
  product's differentiator was invisible and every agent integration had to
  remember to ask for it.
- Deny the script's directory too: stricter, and `import utils` fails on the
  first run; the directory of the file you chose to execute is the smallest
  grant that keeps ordinary scripts working.
- Interactive permission prompts: good terminal experience; deferred until
  the non-interactive contract is stable.
- Namespaces, containers or microVMs: stronger boundaries with separate
  operational costs; candidates for later layers, not replacements.
- Keep a syscall denylist: silently admits new kernel interfaces, leaving the
  effective policy dependent on kernel upgrades.
- Copy the Docker profile unchanged: it assumes container namespaces and
  capabilities that a Pynox worker does not have.
- Return `EPERM` for `clone3`: breaks glibc's fallback and ordinary Python
  threads. Return `EPERM` for x32: changes an existing contract assertion.

## Consequences

The default costs a supervised worker: about 2.5 ms over `python` on a tiny
script, measured, and documented rather than hidden. Scripts that read or
write outside their directory need grants, and `pynox.toml` exists so a
project states them once. The default mode needs Linux 6.2 or newer and an
environment that lets an unprivileged process call `prctl`, `seccomp` and
`pidfd_open`; `pynox doctor` probes these and lists the reasons when the
sandbox cannot run. Measured on 2026-10-18, AWS Lambda offers neither (kernel
5.10, `prctl` denied), so there and in similar microVM services the command
must be `--no-sandbox` explicitly, and the platform's own isolation is the
boundary. Metadata visibility, persistent writes and the absence of domain
allowlists remain disclosed limits. Full hostile-code containment is not
claimed.

Kernel upgrades no longer implicitly grant new syscalls. A new CPython, libc
or native package may need an explicit policy addition; denied native calls
return an errno so Python can report an exception when it handles that error.
This is not a promise that every native library recovers from a denied call.
The x86_64 legacy entry points and aarch64 interfaces are tested separately on
glibc and musl for all five supported CPython minors.

Rust unit tests evaluate emitted BPF and stacked actions without installing
filters in the test process. Existing cold, pool and preload contract tests
remain unchanged. Record paired startup measurements with this change; keep
historical benchmark artifacts intact. Adversarial validation remains a
separate external review.
