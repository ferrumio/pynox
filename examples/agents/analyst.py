"""A data analyst agent (Pydantic AI + Claude on Bedrock) with a sandboxed notebook.

The model answers questions about a CSV by writing Python. Pynox runs that
code in one persistent, sandboxed process: variables survive between tool
calls, `data/` is readable, `out/` is writable, and nothing else is.

    uv run --group agents python -m agents.analyst      # from examples/, with AWS credentials
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pynox
from pydantic_ai import Agent
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider

from agents.common import make_sales, model_id, region

INSTRUCTIONS = """You are a data analyst. Answer by running Python with the run_python tool.
The tool is a notebook: variables persist between calls, so load the data once.
The sales data is at {data}. Write any files you produce under {out}.
Print compact summaries, not whole tables. Finish with a short answer for the user."""

QUESTION = (
    "Which region grew revenue the most from Q1 to Q2, and which product drove it? "
    "Write a short markdown report with a table to out/report.md."
)


def build_agent(root: Path, model=None) -> Agent:
    notebook = pynox.tool(
        session=True,
        allow_read=[root / "data"],
        allow_write=[root / "out"],
        timeout="30s",
        memory="1g",
    )
    if model is None:
        model = BedrockConverseModel(model_id(), provider=BedrockProvider(region_name=region()))
    return Agent(
        model,
        instructions=INSTRUCTIONS.format(data=root / "data" / "sales.csv", out=root / "out"),
        tools=[notebook],
    )


def run(root: Path, question: str = QUESTION, model=None):
    make_sales(root)
    return build_agent(root, model).run_sync(question)


def print_transcript(result) -> None:
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    for message in result.all_messages():
        for part in message.parts:
            if isinstance(message, ModelResponse) and isinstance(part, ToolCallPart):
                print(f"\n--- model runs Python ---\n{part.args_as_dict().get('code', part.args)}")
            elif isinstance(message, ModelRequest) and isinstance(part, ToolReturnPart):
                print(f"--- Pynox returns ---\n{str(part.content).rstrip()}")
    print(f"\n--- answer ---\n{result.output}")
    stats = pynox.default_pool().stats()
    print(
        f"\n--- cost of the sandbox ---\n{stats['jobs']} Python runs, "
        f"{stats['duration_ms_total']} ms inside Pynox in total"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pynox-analyst-") as temporary:
        root = Path(temporary)
        question = " ".join(sys.argv[1:]) or QUESTION
        result = run(root, question)
        print_transcript(result)
        report = root / "out" / "report.md"
        if report.exists():
            print(f"\n--- {report.name} ---\n{report.read_text()}")
