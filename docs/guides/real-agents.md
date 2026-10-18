# Real agents

Three agents built with real frameworks, talking to a real model. Everything on
this page is copied from actual runs: Claude Opus 5.5 through Amazon Bedrock
(`eu.anthropic.claude-opus-5-5`, `eu-west-2`), on 2026-10-18. The code is in
[`examples/agents/`](https://github.com/ferrumio/pynox/tree/main/examples/agents).

A model does not answer the same way twice, so your runs will differ in the
details. What does not change is what Pynox lets the code do.

| Agent | Framework | Python runs | Time inside Pynox | Whole run |
| --- | --- | ---: | ---: | ---: |
| [Data analyst](#data-analyst-pydantic-ai) | Pydantic AI | 5 | 7 ms | 27 s |
| [Support agent](#support-agent-strands-agents) | Strands Agents | 2 | 21 ms | 8 s |
| [Coding agent](#coding-agent-pydantic-ai) | Pydantic AI | 1 test run | under 100 ms | 10 s |

The sandbox costs milliseconds; the model's thinking costs seconds. That is the
point: isolation cheap enough that nobody has to decide whether a tool call is
worth it.

```bash
uv sync --group agents && cd examples
export PYNOX_BIN=$PWD/../target/release/pynox PYTHONPATH=$PWD/../python:.
uv run --group agents python -m agents.analyst
```

## Data analyst (Pydantic AI)

**Asked:** "Which region grew revenue the most from Q1 to Q2, and which product
drove it? Write a short markdown report with a table to out/report.md."

The whole integration is one tool:

```python
notebook = pynox.tool(session=True, allow_read=[root / "data"], allow_write=[root / "out"],
                      timeout="30s", memory="1g")
agent = Agent(model, instructions=INSTRUCTIONS, tools=[notebook])
```

The first thing the model tried:

```text
--- model runs Python ---
import pandas as pd
df=pd.read_csv('/tmp/pynox-analyst-l2tulcai/data/sales.csv')
--- Pynox returns ---
ModuleNotFoundError: No module named 'pandas'
at agent.py:1
```

Nobody granted pandas to this sandbox. The model read the error and switched to
the standard library on its next call:

```text
--- model runs Python ---
import csv
rows=list(csv.DictReader(open('/tmp/pynox-analyst-l2tulcai/data/sales.csv')))
print(len(rows));print(rows[:3])
--- Pynox returns ---
72
[{'date': '2026-01-15', 'region': 'north', 'product': 'widget', 'units': '81', 'revenue': '1012.50'}, ...]
```

`rows` stayed in memory, so the next three calls aggregated, checked and wrote
the report without reloading anything:

```text
--- Pynox returns ---
east 14285.25 11650.42 -2634.83 -18.4% {'gadget': -3912.0, 'gizmo': 224.17, 'widget': 1053.0}
north 10319.5 13311.63 2992.13 29.0% {'gadget': 2527.5, 'gizmo': -290.36, 'widget': 754.99}
south 10072.0 15019.48 4947.48 49.1% {'gadget': 2831.1, 'gizmo': 682.88, 'widget': 1433.5}
west 11117.5 13167.84 2050.34 18.4% {'gadget': 1898.4, 'gizmo': 169.94, 'widget': -18.0}
```

The report it wrote to `out/report.md`:

```markdown
## What drove South's growth

| Product | Q1 | Q2 | Change | Share of South's growth |
|---|---:|---:|---:|---:|
| Gadget | $6,090.00 | $8,921.10 | +$2,831.10 | 57% |
| Widget | $2,300.00 | $3,733.50 | +$1,433.50 | 29% |
| Gizmo | $1,682.00 | $2,364.88 | +$682.88 | 14% |
```

Five Python runs, 7 ms inside Pynox in total. The project's `.env`, one level
above the data, was never readable; to let the agent use pandas, grant the
environment's site-packages with `package_dirs` or a profile in `pynox.toml`.

## Support agent (Strands Agents)

**Asked:** "Hi, this is Ana (ana@example.com). Two things in my last delivery
arrived broken. Can you help?"

The store's backend is two Python methods with one business rule: refunds above
200 need a human. They run in your process; the model's code reaches them only
through `pynox_host`.

```python
run_python = tool(pynox.tool(functions={"find_orders": store.find_orders,
                                        "issue_refund": store.issue_refund}))
agent = Agent(model=BedrockModel(model_id=..., region_name=...), tools=[run_python])
```

The model read the function signatures from the tool description and needed two
runs:

```text
--- model runs Python ---
from pynox_host import find_orders
for o in find_orders("ana@example.com"): print(o)
--- Pynox returns ---
{'id': 'A-100', 'item': 'desk lamp', 'status': 'delivered', 'total': 42.0}
{'id': 'A-101', 'item': 'office chair', 'status': 'damaged', 'total': 310.0}
{'id': 'A-102', 'item': 'usb hub', 'status': 'damaged', 'total': 24.9}

--- model runs Python ---
from pynox_host import issue_refund, HostError
for oid,amt in [("A-101",310.0),("A-102",24.9)]:
    try: print(oid, issue_refund(oid, amt))
    except HostError as e: print(oid, "ERR", e)
--- Pynox returns ---
A-101 ERR PermissionError: refund of 310.00 needs approval; ticket opened for A-101
A-102 refunded 24.90 for A-102
```

```text
--- reply to the customer ---
Hi Ana, I'm sorry two items arrived broken. I've refunded the full $24.90 for the USB hub
(order A-102). Refunds over $200 need approval from a staff member, so I've opened a ticket
for the $310.00 office chair (order A-101), and a member of our team will contact you about it.

--- backend state ---
refunds: [('A-102', 24.9)]
tickets: ['A-101']
```

The model tried the large refund; your rule refused it and opened a ticket,
and the model explained that to the customer. Two runs, 21 ms inside Pynox.

!!! note "What the model sees"
    Strands, like other frameworks that read Google-style docstrings, sends only
    the text before `Args:` as the tool description. `pynox.tool()` puts the
    permissions and every host function's signature there, so the model calls
    `issue_refund(order_id, amount)` correctly the first time instead of guessing.
    The example also retries on a fallback model if the first one declines the
    request, since Bedrock has no server-side fallback.

## Coding agent (Pydantic AI)

**Asked:** "The test suite is failing. Find the bug in the library, fix it, and
confirm the tests pass."

File tools run in your process and refuse paths outside the repository. The test
run, which executes code the model just wrote, goes through Pynox:

```python
def run_tests() -> str:
    """Run the test suite in the Pynox sandbox and return its output."""
    result = subprocess.run(["pynox", "-m", "unittest", "-v"], cwd=repo,
                            capture_output=True, text=True, timeout=60)
    return f"exit {result.returncode}\n{result.stdout}{result.stderr}"
```

```text
--- model calls list_files() ---
--- model calls read_file(path=...) ---
--- model calls read_file(path=...) ---
--- model calls write_file(path=..., content=...) ---
--- model calls run_tests() ---
exit 0
test_tax (test_invoice.InvoiceTest.test_tax) ... ok
test_total (test_invoice.InvoiceTest.test_total) ... ok
OK

--- answer ---
**The bug:** In `invoice.py`, `total()` added each line's quantity and price together
(`line['qty'] + line['price']`) when it should have multiplied them. ...
**The fix:** I changed `+` to `*` on that one line. Nothing else needed to change.
```

In the sandbox the tests can read the project and nothing else, cannot write,
cannot open a socket and cannot start a process. If an edit or a test goes
wrong, it fails as a test; it does not touch your home directory.

## Tested without a model

`tests/test_agent_examples.py` runs the same three agents in CI with scripted
models: Pydantic AI's `FunctionModel` makes fixed tool calls, and the Strands
tool is invoked directly. The framework, the tool schema it builds, the pool,
the sandbox and the host functions are all real; only the model is replaced.

## Next

- [Use cases](use-cases.md): the same patterns without a framework, with the
  overreach cases spelled out
- [Agents](agents.md): every option of `pynox.tool()`
- [Pool protocol](pool.md): what the package sends underneath
