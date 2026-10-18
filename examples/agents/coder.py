"""A coding agent (Pydantic AI + Claude on Bedrock) that fixes a bug and runs the tests.

File tools run in your process and are limited to the repository. The test
suite, which executes code the model just wrote, runs in Pynox: the project is
readable, nothing is writable, there is no network and no subprocess. A test
that deletes the wrong directory fails instead of deleting it.

    uv run --group agents python -m agents.coder        # from examples/, with AWS credentials
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider

from agents.common import model_id, region

TASK = "The test suite is failing. Find the bug in the library, fix it, and confirm the tests pass."

PROJECT = {
    "invoice.py": (
        '"""Invoice totals."""\n\n\n'
        "def total(lines, tax_rate=0.0):\n"
        '    """Sum quantity * price for each line, then add tax."""\n'
        "    subtotal = sum(line['qty'] + line['price'] for line in lines)\n"
        "    return round(subtotal * (1 + tax_rate), 2)\n"
    ),
    "test_invoice.py": (
        "import unittest\n\nfrom invoice import total\n\n\n"
        "class InvoiceTest(unittest.TestCase):\n"
        "    def test_total(self):\n"
        "        self.assertEqual(total([{'qty': 2, 'price': 10.0}, {'qty': 1, 'price': 5.5}]), 25.5)\n\n"
        "    def test_tax(self):\n"
        "        self.assertEqual(total([{'qty': 4, 'price': 2.5}], tax_rate=0.2), 12.0)\n"
    ),
}


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    for name, source in PROJECT.items():
        (repo / name).write_text(source)
    return repo


def build_agent(repo: Path, model=None) -> Agent:
    binary = os.environ.get("PYNOX_BIN", "pynox")

    def inside(path: str) -> Path:
        target = (repo / path).resolve()
        if not target.is_relative_to(repo.resolve()):
            # ModelRetry hands the message back to the model instead of ending the run.
            raise ModelRetry(f"{path} is outside the repository; use a path inside it")
        return target

    def list_files() -> list[str]:
        """List the repository's files."""
        return sorted(str(p.relative_to(repo)) for p in repo.rglob("*.py"))

    def read_file(path: str) -> str:
        """Read a file from the repository."""
        return inside(path).read_text()

    def write_file(path: str, content: str) -> str:
        """Replace a file in the repository with new content."""
        inside(path).write_text(content)
        return f"wrote {path}"

    def run_tests() -> str:
        """Run the test suite in the Pynox sandbox and return its output."""
        result = subprocess.run(
            [binary, "-m", "unittest", "-v"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return f"exit {result.returncode}\n{result.stdout}{result.stderr}"[-4000:]

    if model is None:
        model = BedrockConverseModel(model_id(), provider=BedrockProvider(region_name=region()))
    return Agent(
        model,
        instructions="You are a careful engineer. Read the code, make the smallest fix, run the tests.",
        tools=[list_files, read_file, write_file, run_tests],
        retries=3,
    )


def run(root: Path, task: str = TASK, model=None):
    repo = make_repo(root)
    return repo, build_agent(repo, model).run_sync(task)


def print_transcript(result) -> None:
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    for message in result.all_messages():
        for part in message.parts:
            if isinstance(message, ModelResponse) and isinstance(part, ToolCallPart):
                print(
                    f"\n--- model calls {part.tool_name}({', '.join(f'{k}=...' for k in part.args_as_dict())}) ---"
                )
            elif isinstance(message, ModelRequest) and isinstance(part, ToolReturnPart):
                if part.tool_name == "run_tests":
                    print(str(part.content).rstrip())
    print(f"\n--- answer ---\n{result.output}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pynox-coder-") as temporary:
        repo, result = run(Path(temporary))
        print_transcript(result)
        print(f"\n--- invoice.py after the fix ---\n{(repo / 'invoice.py').read_text()}")
