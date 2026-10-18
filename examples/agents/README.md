# Real agents on Pynox

Three agents built with real agent frameworks and Claude. The model writes the
Python; Pynox runs it in a kernel sandbox, with the permissions each example
grants and nothing else.

| Agent | Framework | What Pynox does for it |
| --- | --- | --- |
| [`analyst.py`](analyst.py) | Pydantic AI | A notebook-style session: variables survive between calls, `data/` readable, `out/` writable |
| [`support.py`](support.py) | Strands Agents | Host functions: the model's code calls your `find_orders` and `issue_refund`; your rules hold |
| [`coder.py`](coder.py) | Pydantic AI | Runs the test suite the model just edited, read-only, no network, no subprocesses |

## Run them

They call Claude through Amazon Bedrock, so any AWS credentials with Bedrock access work.

```bash
make build
uv sync --group agents
cd examples
export PYNOX_BIN=$PWD/../target/release/pynox PYTHONPATH=$PWD/../python:.
uv run --group agents python -m agents.analyst
uv run --group agents python -m agents.support
uv run --group agents python -m agents.coder
```

| Variable | Default |
| --- | --- |
| `PYNOX_AGENT_MODEL` | `eu.anthropic.claude-opus-5-5` |
| `PYNOX_AGENT_FALLBACK_MODEL` | `eu.anthropic.claude-opus-4-8`, used if the first model declines |
| `AWS_REGION` | `eu-west-2` |

Each run prints what the model wrote, what Pynox returned, the final answer and
how long the sandboxed runs took. `tests/test_agent_examples.py` drives the same
agents with scripted models, so CI checks them without an API key.

See [Real agents](https://ferrumio.github.io/pynox/guides/real-agents/) in the
docs for transcripts from real runs.
