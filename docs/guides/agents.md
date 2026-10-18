# Agents

An agent answers a question by writing Python and running it. This guide
shows where that code runs with Pynox, who decides what it may touch, what the
model gets back, and how to plug it into a framework.

## The problem in four lines

```python
def run_python(code: str) -> str:
    return subprocess.run(["python", "-c", code], capture_output=True, text=True).stdout
```

This is how most agents run code today. It runs with your privileges. An
ambiguous request becomes `rmtree` on the wrong directory; a "let me check
the config" becomes a read of `~/.aws/credentials`; a `while True` hangs the
agent. The alternative, a remote container, costs a second per call and a
bill per hour.

With Pynox the function keeps its shape and loses the risk: the code runs in
a fresh process under a kernel policy you wrote, in about 5 ms, and the model
reads back exactly what went wrong when it oversteps.

## Install

```bash
make wheel && pip install dist/pynox-*.whl     # from a checkout; PyPI follows the first release
```

The wheel is tagged for one CPython minor and carries a matching CPython inside,
so `pip install` in a 3.12 environment gives you a sandbox that runs 3.12 and can
import that environment's packages through `package_dirs`.

## The tool

```python
import pynox

run_python = pynox.tool(profile="analysis")
```

That is a plain function, `run_python(code: str) -> str`, with a docstring
written for the model. Its only parameter is `code`; paths, network, time and
memory come from the profile in `pynox.toml` or from keyword arguments you pass
to `pynox.tool()`. If the model wants more access, it has to ask you.

```python
run_python = pynox.tool(allow_read=["data"], allow_write=["out"], timeout="10s", memory="256m")
```

Behind it is one pool for the process, started on first call and closed at
exit. Add `session=True` and the tool keeps one isolated process, so a variable
defined in one call is there in the next, like a notebook:

```python
notebook = pynox.tool(session=True, allow_read=["data"])
notebook("import pandas as pd\ndf = pd.read_csv('data/orders.csv')")
notebook("print(df.describe())")                 # df is still there
```

A Python error leaves the session alive; a timeout or a crash ends it and the
next call starts a fresh process.

Give the code your application's own functions and the model can chain them
in one run instead of one tool call per turn:

```python
def search(query: str, limit: int = 5) -> list[dict]:
    """Search the product catalog."""
    return catalog.search(query, limit)

run_python = pynox.tool(functions={"search": search}, allow_write=["out"])
```

The docstring the model reads lists `search(query: str, limit: int = 5)`, and
its code calls it with `from pynox_host import search`. The function runs in
your process, not in the sandbox: it can reach your database or API while the
model's code still cannot. Arguments and results travel as JSON; an exception
in your function raises `pynox_host.HostError` in the model's code. `Pool.session()` gives the same thing as an
object with `run`, `close`, `alive` and `steps`. For several permission sets or
explicit lifecycle, build pools yourself:

```python
analysis = pynox.Pool(workers=2, preload=["pandas"], config="pynox.toml")
run_analysis = analysis.tool(profile="analysis")
run_fetch = analysis.tool(profile="fetch")
```

The complete example in the repository, exercised by the test suite:

??? example "examples/agent_tool.py"
    ```python
    --8<-- "examples/agent_tool.py"
    ```

## What the model gets back

`pynox.run()` and `Pool.run()` return a `Result`; `tool()` returns its `text`:

| Situation | Text returned |
| --- | --- |
| Success | stdout, or `(no output)` |
| Python exception | `TypeName: message`, then `at name.py:line`, then a hint when one applies, then stderr |
| Timeout | `TimeoutError: Wall time exceeded 10000ms` |
| Killed by a limit | `exit status 137` and stderr |

The structured form is there when you want to branch on it instead:

```python
result = pynox.run(code)
result.ok              # exit == 0 and no error
result.text            # what tool() hands the model
result.exit            # Python exit status; 124 timeout, 125 setup failure
result.error           # None or {"type", "message", "file", "line", "path", "hint"}
result.stdout, result.stderr, result.truncated, result.duration_ms, result.policy
```

Error locations use the request's `name` (default `agent.py`), so a model that
numbers its lines can map them back. Hints are written for models: a
`PermissionError` says which flag would grant the path; a `MemoryError` says to
reduce allocations or raise `memory`.

## Permissions model

| Capability | Default | How to grant |
| --- | --- | --- |
| Read a file or directory | denied, except the standard library | `allow_read=[path]` |
| Write beneath a path | denied | `allow_write=[path]`; does not imply read |
| Import installed packages | denied | `package_dirs=[site_packages]` or pool `--preload` |
| Environment variables | none | `env={"KEY": value}` |
| Network | denied | `allow_network=True` (IPv4/IPv6, any host) |
| Subprocesses, host Unix sockets | denied | not grantable |
| Wall time, address space, output | 10 s, 512 MiB, 64 KiB | `timeout`, `memory`, `max_output` |
| Size of any written file | unlimited | `max_file_size`, or a `[pool.limits]` ceiling |

Enforcement is in the kernel (Landlock and seccomp) and covers native code,
ctypes included. Read the [sandbox contract](../sandbox.md) before relying on
it: it is controlled automation, not a boundary for hostile code.

## Registering with a framework

[Real agents](real-agents.md) has complete Pydantic AI and Strands Agents
examples, tested in CI, with transcripts from real runs.


Any framework that accepts a typed, documented Python function accepts
`pynox.tool()`. The snippets show the shape; `examples/agent_tool.py` and
`examples/monty_bridge.py` run in Pynox's test suite, the framework lines do
not yet.

=== "Pydantic AI"
    ```python
    from pydantic_ai import Agent
    import pynox

    agent = Agent("anthropic:claude-sonnet-4-5", tools=[pynox.tool(profile="analysis")])
    ```

=== "Strands"
    ```python
    from strands import Agent, tool
    import pynox

    agent = Agent(tools=[tool(pynox.tool(profile="analysis"))])
    ```

=== "smolagents"
    ```python
    from smolagents import CodeAgent, tool
    import pynox

    agent = CodeAgent(tools=[tool(pynox.tool(profile="analysis"))], model=model)
    ```

=== "Your own loop"
    ```python
    import pynox

    tools = {"run_python": pynox.tool(profile="analysis")}
    # When the model calls run_python, pass its `code` argument and append
    # the returned text as the tool result.
    ```

For agents whose glue code should itself run isolated, pair Pynox with
[Monty](monty.md).

## Patterns

**Profiles instead of ad-hoc permissions.** Put the permission sets your
application needs in `pynox.toml` as `[profiles.NAME]`, start the pool with
`--config`, and pass `profile=` per call. `[pool.limits]` caps every request,
so a bug in your tool code cannot widen access beyond what the operator set.

**Preload what every task imports.** `PynoxPool(preload=["pandas"])` pays the
import once in the template; each forked child starts with it loaded.

**Keep outputs small.** `max_output` bounds what is captured; the model gets
the first bytes and a `truncated` flag. Ask the model to print summaries.

**Treat the result as data, not as an audit log.** The task can write to its
own report channel. Use the kernel policy for guarantees, the report for
diagnostics.

## Latency

Measured on this project's Linux ARM64 host, p50, request to result:

| Task | `pynox pool` | New `python` process |
| --- | ---: | ---: |
| Tiny script | 4.68 ms | 7.68 ms |
| 200k-iteration loop | 14.07 ms | 16.95 ms |
| JSON pipeline, 3.89 MiB | 75.75 ms | 153.80 ms |

See [performance](../performance.md) for the method and the full table.

## Next steps

- [Use cases](use-cases.md): four agents end to end, with real output
- [Pool protocol](pool.md): every field of a request and a result
- [Monty](monty.md): Monty orchestrates, Pynox executes
- [Sandbox contract](../sandbox.md): what the kernel enforces and what it does not
