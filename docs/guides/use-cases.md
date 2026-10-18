# Use cases

!!! tip "Want to see a real model do it?"
    [Real agents](real-agents.md) runs the analyst, support and coding agents with
    Pydantic AI, Strands Agents and Claude, with transcripts from real runs.

Five agents people build with Pynox. Each one is a runnable file in
`examples/use_cases/`, exercised by the test suite on every commit, so the
output below is what the code prints. The model's code is fixed text in each
example, the kind of code a model writes for that request, so you can run
them without an API key.

```bash
PYNOX_BIN=$PWD/target/release/pynox PYTHONPATH=python:examples \
  uv run python -m use_cases.data_analyst
```

| Agent | What Pynox gives it |
| --- | --- |
| [Data analyst](#data-analyst) | Notebook-style state across turns, reads `data/`, writes `out/`, nothing else |
| [Support agent](#support-agent) | Calls your backend functions; your rules hold, your database stays out of reach |
| [Coding agent](#coding-agent) | Runs the project's tests; a test that deletes the wrong directory fails instead |
| [API agent](#api-agent) | Network and an API key for one tool only, granted by you |
| [Two-level agent](#two-level-agent-with-monty) | Monty runs the plan, Pynox runs the heavy code |

## Data analyst

**The request:** "Which region grew the most last quarter? Write a report."

The model explores the data over several turns, the way a person uses a
notebook. It loads the CSV once and keeps `rows` and `growth` in memory between
calls, so later turns are fast and short.

```python
import pynox

notebook = pynox.tool(session=True, allow_read=["data"], allow_write=["out"], timeout="20s")

agent = Agent("anthropic:claude-sonnet-4-5", tools=[notebook])   # Pydantic AI, for example
```

What the tool returns to the model, turn by turn:

```text
>>> rows = list(csv.DictReader(open(DATA))) ...
400 rows; columns: region, quarter, revenue

>>> growth = {region: (q2 - q1) / q1 ...}; best = max(growth, key=growth.get) ...
south grew 31.0%

>>> with open(OUT, 'w') as f: ...
report written

>>> print(open(ENV).read())          # "let me check the data source configuration"
PermissionError: [Errno 13] Permission denied: '/work/.env'
at agent.py:1
For files, grant the specific path with --allow-read/--allow-write. ...
```

The report lands in `out/report.md`. The project's `.env`, one directory up
from the data, never reaches the model: the kernel denied the read, and the
model read why. Add `preload=["pandas"]` to a `pynox.Pool` and the same agent
imports pandas at no cost per turn.

??? example "examples/use_cases/data_analyst.py"
    ```python
    --8<-- "examples/use_cases/data_analyst.py"
    ```

## Support agent

**The request:** "Ana says an order arrived damaged. Sort it out."

The agent needs your order system. Instead of one tool call per lookup, its
code calls your functions directly and chains them, the pattern known as
CodeAct. The functions run in your process, outside the sandbox, with your
database connection and your business rules.

```python
def find_orders(customer: str) -> list[dict]:
    """List a customer's orders with id, total and status."""

def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order. Amounts above 200 need a human."""

run_python = pynox.tool(functions={"find_orders": find_orders, "issue_refund": issue_refund})
```

The model reads those signatures in the tool's description and writes:

```python
from pynox_host import find_orders, issue_refund, HostError

orders = find_orders("ana@example.com")
damaged = [o for o in orders if o["status"] == "damaged"]
for order in damaged:
    try:
        print(issue_refund(order["id"], order["total"]))
    except HostError as error:
        print(f"{order['id']}: {error}")
```

```text
A-101: PermissionError: refunds above 200 need approval; ticket opened
checked 2 orders, 1 damaged
```

Your rule held: no refund was issued. Then the model tries a shortcut and opens
the database itself:

```text
OperationalError: unable to open database file
at agent.py:2
```

The code can only reach your system through the functions you handed it.

??? example "examples/use_cases/support_agent.py"
    ```python
    --8<-- "examples/use_cases/support_agent.py"
    ```

## Coding agent

**The request:** "Fix the bug in `calc.py` and make sure the tests pass."

Coding agents run test suites dozens of times per task, on code they just
wrote. Here a test "cleans up fixtures" with the wrong path:

```python
def test_cleanup_gone_wrong(self):
    shutil.rmtree(os.path.expanduser('~/important-notes'))
```

```bash
pynox -m unittest -q
```

```text
ERROR: test_cleanup_gone_wrong (test_calc.CalcTest.test_cleanup_gone_wrong)
  ...
PermissionError: [Errno 13] Permission denied: '/home/you/important-notes'
Ran 2 tests in 0.001s
FAILED (errors=1)
```

The real test passed, the bad one failed loudly, and `~/important-notes` is
still there. `-m` makes the project directory readable and nothing writable;
add `--allow-write=build` for a build directory. It is the same command a
developer types, so the agent's tool is one line:

```python
subprocess.run(["pynox", "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=repo, capture_output=True)
```

??? example "examples/use_cases/coding_agent.py"
    ```python
    --8<-- "examples/use_cases/coding_agent.py"
    ```

## API agent

**The request:** "What's the weather in Lisbon?"

The model writes ordinary `urllib` code and reads the key from the
environment. You decide which tool gets the network and which variable it
sees; the key never appears in the prompt or the model's code.

```python
fetch = pynox.tool(allow_network=True, env={"WEATHER_KEY": os.environ["WEATHER_KEY"]})
think = pynox.tool()   # same code, no network
```

```text
fetch  →  {'city': 'Lisbon', 'temp_c': 21}
think  →  URLError: <urlopen error [Errno 1] Operation not permitted>
```

Depending on the platform, the denial can also arrive as
`PermissionError: [Errno 13] Permission denied`; either way the socket never
opens.

Network access is all or nothing today: a tool with the grant can reach any
IPv4/IPv6 host, though never a subprocess or a host Unix socket. Give it only
to the tools that need it. Per-domain allowlists are planned.

??? example "examples/use_cases/api_agent.py"
    ```python
    --8<-- "examples/use_cases/api_agent.py"
    ```

## Two-level agent with Monty

**The request:** "Average these readings and store them."

[Monty](https://github.com/pydantic/monty) runs the agent's plan in a tiny
interpreter that can pause, snapshot and resume. When the plan needs real
Python, it calls `run_python`, and Pynox runs that part with the full
ecosystem and the permissions the host fixed.

```text
mean: 13
stored: {"mean": 13, "count": 3}
```

See [Monty](monty.md) for the walkthrough and the code.

## What every case shares

- **The model only writes code.** Paths, network, keys, time and memory are
  arguments you pass to `pynox.tool()` or profiles in `pynox.toml`.
- **The kernel enforces the policy.** Landlock and seccomp apply to Python and
  to native code alike; there is no wrapper to talk around.
- **Failures are information.** The model gets the exception, the line and a
  hint, and its next attempt is usually right.
- **It is fast enough to call freely.** About 5 ms per call from a warm pool,
  so an agent can run code twenty times in a conversation without anyone
  noticing.

Read the [sandbox contract](../sandbox.md) for exactly what is enforced. Pynox
controls automation; it is not a boundary for deliberately hostile code.
