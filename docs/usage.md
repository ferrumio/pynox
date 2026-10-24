# Commands

One binary, one default: `pynox` runs Python in the sandbox and behaves like
`python` at the terminal. Flags grant permissions, `--json` changes the output
format, `--no-sandbox` turns the sandbox off.

## `pynox [FLAGS] SCRIPT | -c CODE | -m MODULE [ARGS]`

```bash
pynox app.py --flag value
pynox -c 'print("hi")'
pynox -m pytest -q
pynox --allow-read=data --allow-write=out --timeout=5s job.py
```

Pynox's flags are long options CPython never uses, so they go first; everything
from the program onward is Python's. stdin, stdout and stderr are the user's,
tracebacks look like Python's and exit codes are Python's. A permission or
memory error adds one `pynox:` line with the hint; a timeout or launcher
failure prints one `pynox:` line and exits 124 or 125.

| Flag | Meaning |
| --- | --- |
| `--allow-read=PATH` | Read an existing file or directory; repeatable |
| `--allow-write=PATH` | Write beneath an existing path; does not grant reads |
| `--package-dir=PATH` | Import installed packages from this directory; no `.pth` execution |
| `--allow-env=NAME` | Forward this environment variable; native-loader variables are refused |
| `--allow-network` | IPv4/IPv6, including listen and outbound; subprocesses and host Unix sockets stay denied |
| `--timeout=5s` | Wall-clock limit, up to 24h |
| `--memory=512m` | Address-space limit, minimum 32m |
| `--max-output=65536` | Captured bytes per stream with `--json`, up to 16 MiB |
| `--max-file-size=SIZE` | Largest size any written file may reach, e.g. `100m`; unlimited by default |
| `--config=PATH` | `pynox.toml` to read; default `$PYNOX_CONFIG`, then `./pynox.toml` |
| `--profile=NAME` | Apply a named profile from the configuration |
| `--json` | Print one JSON result instead of passing output through |
| `--no-sandbox` | CPython's own command line, unrestricted; no other flag applies |

Everything is denied unless granted, with one exception: the directory of the
script you run is readable, so sibling imports work. `-m` makes the current
directory readable instead, as `sys.path[0]` would; `-c` grants nothing.
Reads never imply writes and writes never imply reads.

Inside the sandbox the program is a script, `-c` or `-m`. The interpreter
flags `-I -E -s -S -u -B -q` are accepted as no-ops; other interpreter options
need `--no-sandbox`. Streams use normal CPython buffering. Use
`print(..., flush=True)` for immediate output, or `--no-sandbox -u` for
CPython's unrestricted, unbuffered CLI.

## `--json`

```bash
pynox --json --allow-read=data job.py
```

```json
{"exit": 0, "stdout": "...", "stderr": "", "stdout_truncated": false, "stderr_truncated": false,
 "error": null, "duration_ms": 9,
 "runtime": {"name": "pynox", "python": "3.11.17", "output": "captured"},
 "policy": {"allow_read": ["/abs/data"], "allow_write": [], "implicit_read": ["/abs/project"],
            "package_dirs": [], "allow_env": [], "network": "deny", "subprocesses": "deny",
            "host_unix_sockets": "deny", "timeout_ms": 5000, "memory_bytes": 536870912,
            "backend": "landlock+seccomp"}}
```

`error` is `null` or `{"type", "message", "file", "line", "path", "hint"}`.
Exit 124 means timeout; 125 means launcher or setup failure. stdin is
`/dev/null`. A script can write to its own diagnostic channel; the result is
for automation, not a security audit record.

## `--no-sandbox`

```bash
pynox --no-sandbox -O app.py
pynox --no-sandbox -m venv .venv
pynox --no-sandbox            # the REPL
```

CPython's command line, unchanged, with Pynox's optimizations installed. No
permissions, capture or limits; it inherits the environment like `python`.
Use it on kernels without Landlock ABI 3, for interpreter options the sandbox
does not map, and for anything that must behave exactly like `python`.

## `pynox.toml`

A project or server states its permissions once:

```toml
[permissions]                      # defaults for sandboxed command-line runs
allow_read = ["data"]
allow_write = ["out"]
allow_env = ["API_KEY"]
timeout = "10s"

[profiles.fetch]                   # pynox --profile=fetch job.py
allow_network = true

[pool]                             # pynox pool --config pynox.toml
workers = 4
preload = ["pandas"]
max_sessions = 16                  # persistent sessions, see the pool protocol
session_idle = "10m"

[pool.limits]                      # no request may exceed these
allow_read = ["data"]
allow_write = ["out"]
allow_network = false
timeout = "30s"
memory = "1g"
```

Discovery: `--config PATH`, then `PYNOX_CONFIG`, then `./pynox.toml` in the
current directory. A file beside the script is never read, so a program that
can write its own directory cannot widen its permissions. Relative paths are
relative to the file. Flags add to the configured permissions; a profile is
applied between the two. The file is rejected if it lies under a path it makes
writable, and unknown keys are errors. The pool side is described in the
[pool protocol](guides/pool.md#configuration).

## `pynox pool --workers=2`

A warm CPython template that forks a fresh, sandboxed child per JSONL request.
Requests carry inline `code` or a `script`, a `profile` and the same
permissions and limits as the command line, capped by `[pool.limits]`. The
full reference is the [pool protocol](guides/pool.md); for turning it into a
tool for a model see [Agents](guides/agents.md).

## Diagnostics

`pynox doctor` prints build Python, architecture, platform tag, the Landlock
ABI, and `sandbox_supported` with `sandbox_unavailable_reasons`: it probes
Landlock, `prctl` and `pidfd_open` without changing its own process, so a
hardened container or AWS Lambda is diagnosed before a run fails. Actual
sandboxed execution still checks all sandbox setup steps.
`--version` and `-V` report both Pynox and the loaded CPython runtime, without
initializing Python:

```text
pynox 0.1.0 (CPython 3.11.17)
```
