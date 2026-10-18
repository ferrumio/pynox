# Pynox 🐍⚙️

![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)
[![CI](https://github.com/ferrumio/pynox/actions/workflows/ci.yml/badge.svg)](https://github.com/ferrumio/pynox/actions/workflows/ci.yml)
[![Documentation](https://img.shields.io/badge/docs-ferrumio.github.io%2Fpynox-indigo)](https://ferrumio.github.io/pynox/)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/ferrumio/pynox/badge)](https://scorecard.dev/viewer/?uri=github.com/ferrumio/pynox)
[![CPython 3.11–3.15](https://img.shields.io/badge/CPython-3.11%E2%80%933.15-blue)](#python-versions)
![Linux x86_64 and ARM64](https://img.shields.io/badge/platform-Linux%20x86__64%20%7C%20ARM64-lightgrey)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

**A Python with the sandbox built in. Made for the code your agents write.**

Pynox is a Python. One Linux binary carries a complete CPython, so
`pynox app.py` runs the same file that `python app.py` runs, with the same
output and exit code. What changes is around the interpreter: every run gets
only the files, network, time and memory you grant, enforced by the kernel.
A warm pool runs code in a few milliseconds and reports back in a form a
model can act on.

> 🚧 **Pre-alpha (0.1.0).** Linux only. Ready to try and to break; interfaces
> and sandbox policies still change.

Give an agent a Python tool in one line. The model writes code; you decide
what that code may touch:

```python
import pynox

agent = Agent("anthropic:claude-sonnet-4-5", tools=[pynox.tool(allow_read=["data"], allow_write=["out"])])
```

| Agent | What happens when it oversteps |
| --- | --- |
| [Data analyst](docs/guides/use-cases.md#data-analyst) with notebook-style state | Its read of the project's `.env` is denied; the report still lands in `out/` |
| [Support agent](docs/guides/use-cases.md#support-agent) calling your backend | Your refund rule holds; opening the database directly fails |
| [Coding agent](docs/guides/use-cases.md#coding-agent) running the test suite | A test that deletes `~/important-notes` fails instead of deleting it |
| [API agent](docs/guides/use-cases.md#api-agent) with a key | Only the tool you granted gets the network and the key |

Each case is a runnable file in [`examples/use_cases/`](examples/use_cases/),
run by the test suite on every commit. [`examples/agents/`](examples/agents/)
has the same agents built with Pydantic AI and Strands Agents on Claude; in a
[real run](docs/guides/real-agents.md), a complete analysis took five Python runs
and 7 ms inside Pynox. The rest of this page starts from plain Python and
builds up to agents.

## Run a script

Build once (Linux, Rust and uv >= 0.13.1; `uv` fetches the matching Python):

```bash
make setup && make build
export PATH="$PWD/target/release:$PATH"
```

Write `hello.py`:

```python
name = "Pynox"
print(f"Hello from {name}!")
```

```bash
pynox hello.py
# Hello from Pynox!
```

That is all a script needs. It ran inside the sandbox, and it could read its
own directory and the standard library, nothing else.

## See the sandbox work

```bash
echo "print(open('/etc/hostname').read())" > probe.py
pynox probe.py
```

```text
Traceback (most recent call last):
  File "/home/you/probe.py", line 1, in <module>
    print(open('/etc/hostname').read())
PermissionError: [Errno 13] Permission denied: '/etc/hostname'
pynox: For files, grant the specific path with --allow-read/--allow-write. ...
```

Python's traceback, plus one line from Pynox telling you what to do. Grant the
path and run again:

```bash
pynox --allow-read=/etc/hostname probe.py
```

Reads and writes are separate grants. Network is off until you ask.
Subprocesses never run. `-c` and `-m` work the same way: `pynox -m pytest -q`
runs your tests with the current directory readable and nothing writable.

## Say it once

Flags are for one run. A project puts its permissions in `pynox.toml`, in the
directory you run from:

```toml
[permissions]
allow_read = ["data"]
allow_write = ["out"]
timeout = "10s"

[profiles.fetch]
allow_network = true
allow_env = ["API_KEY"]
```

```bash
pynox job.py                    # data readable, out writable
pynox --profile=fetch sync.py   # plus network and the API key
```

Pynox never reads a `pynox.toml` beside the script, only the one where you
run or the one you pass with `--config`. A program cannot widen its own
permissions.

## Agents write code

An agent answers a question by writing Python and running it. Today that code
runs in one of two places. On your machine with your privileges, where one
ambiguous instruction becomes `rmtree` on the wrong directory or a read of
`~/.aws/credentials`. Or in a remote container, which takes a second to start,
costs money per hour and ships your data out.

Pynox is the third place. The model's code runs locally, under the policy you
wrote, in about 5 ms per call. When it oversteps, the model reads what was
denied and why, and its next attempt is usually right:

```python
>>> run_python = pynox.tool(allow_write=["out"], timeout="10s")
>>> print(run_python("print(sum(range(10)))"))
45
>>> print(run_python("open('/etc/passwd').read()"))
PermissionError: [Errno 13] Permission denied: '/etc/passwd'
at agent.py:1
For files, grant the specific path with --allow-read/--allow-write. ...
```

`pynox.tool()` returns a plain `run_python(code) -> str` with a docstring
written for the model, so every framework takes it: Pydantic AI as is,
Strands and smolagents through their `tool` decorator, Monty as an external
function. Three options cover most agents:

- **`session=True`** keeps one isolated process, so a variable defined in one
  call is there in the next, like a notebook.
- **`functions={"search": search}`** lets the model's code call your own
  functions. They run in your process, outside the sandbox, so they can reach
  your database while the model's code cannot.
- **`profile="analysis"`** takes the permissions from `pynox.toml`, so the
  application code carries no paths.

`pynox.run(code)` does the same without an agent and returns a result with
`ok`, `text`, `stdout` and a structured `error`. Each call forks a fresh
process from a warm CPython template: preloaded libraries cost nothing per
call and nothing leaks between calls.

Install the package from a checkout with `make wheel` and
`pip install dist/pynox-*.whl`; PyPI publication follows the first release.
The wheel embeds a CPython of the **same minor version as your environment**,
so the sandbox imports the packages you already installed. Underneath, the
package speaks `pynox pool`, a JSON Lines protocol you can drive from any
language.

Read the [use cases](docs/guides/use-cases.md), the
[agents guide](docs/guides/agents.md) for permissions and results, the
[pool protocol](docs/guides/pool.md) for every field, and
[Monty](docs/guides/monty.md) to pair Pynox with Pydantic's glue-code sandbox.

## Plain Python when you need it

```bash
pynox --no-sandbox -O app.py
pynox --no-sandbox -m venv .venv
pynox --no-sandbox              # the REPL
```

`--no-sandbox` hands the command line to CPython unchanged. Use it for
interpreter options the sandbox does not map, and on kernels that cannot
sandbox, such as AWS Lambda today. `pynox doctor` tells you which case you are
in and why.

## Speed, measured

Pynox makes specific operations faster and says so with numbers. Today it
ships one such path: eligible `json.dump` calls use CPython's C encoder with a
single write, installed only when a program imports `json`. End-to-end p50 on
this project's Linux ARM64 host, CPython 3.11:

| Workload | CPython | `pynox` (sandboxed) | `pynox --no-sandbox` | `pynox pool` |
| --- | ---: | ---: | ---: | ---: |
| Tiny script | 7.45 ms | 10.06 ms | 7.67 ms | 4.47 ms |
| JSON pipeline, 3.89 MiB | 153.63 ms | 81.52 ms | 78.46 ms | 74.48 ms |

The sandbox costs about 2.5 ms per run. Python loops run at CPython speed;
Pynox does not change the interpreter. See [performance](docs/performance.md).

## Platforms and Python versions

| Platform | Architectures |
| --- | --- |
| Linux glibc 2.28+ | x86_64, ARM64 |
| Linux musl 1.2+ / Alpine | x86_64, ARM64 |

The sandbox needs Linux 6.2 or newer (Landlock ABI 3). Containers inherit the
host kernel; AWS Lambda cannot sandbox today. [Deploy](docs/deploy.md) has the
measured details. On macOS, use a Linux container or VM.

Each binary embeds one CPython, 3.11 by default:

```bash
make build PYNOX_PYTHON_VERSION=3.14
pynox --version
# pynox 0.1.0 (CPython 3.14.8)
```

`make bundle` produces a self-contained archive with Pynox and its Python;
`bash scripts/install.sh --archive dist/pynox-*.tar.gz` installs it. See
[installation](docs/distribution.md).

## Documentation

Full documentation: **[ferrumio.github.io/pynox](https://ferrumio.github.io/pynox/)**.

- [Getting started](docs/getting-started.md)
- [Real agents](docs/guides/real-agents.md), [use cases](docs/guides/use-cases.md), [agents](docs/guides/agents.md), [pool protocol](docs/guides/pool.md), [Monty](docs/guides/monty.md)
- [Commands](docs/usage.md) and the [sandbox contract](docs/sandbox.md)
- [Deploy](docs/deploy.md), [installation](docs/distribution.md), [performance](docs/performance.md)
- [Architecture decisions](ADR/README.md) and [security scope](SECURITY.md)

## Contributing

Try your scripts, report what surprises you, and bring real workloads to
measure. The [roadmap](https://github.com/ferrumio/pynox/issues/33) shows what
comes next, and [good first issues](https://github.com/ferrumio/pynox/labels/good%20first%20issue)
are scoped to be picked up in an afternoon, by you or by your coding agent.
Comment on an issue before you start so nobody duplicates work.

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup and checks; `make test` runs the
Rust and Python suites. Rust lives in `src/`, the `pynox` Python package in
`python/pynox/` and the embedded runtime helpers in `python/pynox/_embedded/`.

## License

[Apache 2.0](LICENSE). Bundles retain CPython's upstream licenses.
