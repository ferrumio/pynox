# Pynox

A Python with the sandbox built in. Made for the code your agents write.

!!! warning "Pre-alpha (0.1.0)"
    Linux only, x86_64 and ARM64. Ready to try and to break; interfaces and
    sandbox policies still change.

## Start here

Pynox is a Python. One binary carries a complete CPython, so `pynox app.py`
runs the same file `python app.py` runs, with the same output and exit code.
Around the interpreter, Pynox adds three things, in the order you will meet
them:

1. **A sandbox on every run.** The script reads its own directory and the
   standard library. Everything else needs a grant: a flag for one run, a
   `pynox.toml` for a project. When the script oversteps, you get Python's
   traceback plus one line that says which grant would fix it.
2. **A pool for agents.** Code written by a model runs in a fresh process
   forked from a warm template, under the permissions you chose, in about
   5 ms, and comes back as a result the model can act on.
3. **Optimizations that need no flag.** Pynox speeds up specific operations
   and publishes the measurements. Today: `json.dump`, 1.9× on a 3.9 MiB file.

`--no-sandbox` is the plain interpreter whenever you need it.

## What you can build

| Agent | The request | What happens |
| --- | --- | --- |
| [Data analyst](guides/use-cases.md#data-analyst) | "Which region grew the most?" | Keeps variables across turns, writes the report to `out/`; its read of `.env` is denied |
| [Support agent](guides/use-cases.md#support-agent) | "Ana's order arrived damaged." | Calls your `find_orders` and `issue_refund`; your refund rule holds, the database stays out of reach |
| [Coding agent](guides/use-cases.md#coding-agent) | "Fix the bug and run the tests." | The tests run; the one that deletes `~/important-notes` fails instead |
| [API agent](guides/use-cases.md#api-agent) | "What's the weather in Lisbon?" | Only the tool you granted gets the network and the key |

Each one is a runnable example tested on every commit. [Real agents](guides/real-agents.md)
shows the same patterns with Pydantic AI, Strands Agents and Claude, copied from
real runs: five Python runs for a full analysis cost 7 ms inside Pynox.

## Three ways to run

```bash
pynox report.py                 # your script, sandboxed, output and exit code are Python's
pynox --json report.py          # the same run as one JSON result, for automation
pynox --no-sandbox -O report.py # CPython's own command line, unrestricted
```

For a model's code, `pynox.tool()` from the `pynox` package returns a
function any agent framework registers as a tool; `pynox.run(code)` does the
same from plain Python. The [agents guide](guides/agents.md) shows both.

## Why it exists

An agent answers a question by writing Python and running it. Until now that
code ran with your privileges, where one ambiguous instruction becomes
`rmtree` on the wrong directory, or in a remote container that costs a second
and a bill per call. Pynox puts kernel isolation between the model and your
machine and keeps the whole Python ecosystem on the model's side of it.

## Guides

### Agents

| Guide | What you learn |
| --- | --- |
| [Real agents](guides/real-agents.md) | Pydantic AI and Strands agents on Claude, transcripts from real runs |
| [Use cases](guides/use-cases.md) | Data analyst, support, coding and API agents, with real output |
| [Agents](guides/agents.md) | Turn the pool into a tool; permissions, results, errors, frameworks |
| [Pool protocol](guides/pool.md) | Every request and result field; profiles and limits |
| [Monty](guides/monty.md) | Monty orchestrates, Pynox executes |

### Runtime

| Guide | What you learn |
| --- | --- |
| [Getting started](getting-started.md) | Build, run, see the sandbox act, write `pynox.toml` |
| [Commands](usage.md) | Flags, `--json`, `--no-sandbox`, configuration, `doctor` |
| [Sandbox contract](sandbox.md) | What is granted, what is denied, what remains |
| [Performance](performance.md) | Optimizations, startup budget, measurements |

### Project

| Guide | What you learn |
| --- | --- |
| [Installation](distribution.md) | Bundles, platforms, Python versions |
| [Deploy](deploy.md) | Docker, Kubernetes and Lambda, measured |
| [Research](research.md) | Where the earlier experiments live |
| [Architecture decisions](adr/README.md) | The twelve records behind the design |
