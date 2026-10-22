"""The real-agent examples, driven by scripted models so CI needs no API key or network.

Pydantic AI's FunctionModel plays the model's part with fixed tool calls; Strands
gets a scripted ``Model`` provider that emits the same stream events BedrockModel
would. Everything else is real: the framework, the agent loop, the tool schema it
builds from pynox.tool(), the pool, the sandbox and the host functions.
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import test_runtime as runtime

sys.path.insert(0, str(runtime.ROOT / "python"))
sys.path.insert(0, str(runtime.ROOT / "examples"))
os.environ["PYNOX_BIN"] = str(runtime.BINARY)

HAS_PYDANTIC_AI = importlib.util.find_spec("pydantic_ai") is not None
HAS_STRANDS = importlib.util.find_spec("strands") is not None


def scripted(steps):
    """A FunctionModel that makes the given tool calls in order, then answers."""
    from pydantic_ai.messages import (
        ModelResponse,
        RetryPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )
    from pydantic_ai.models.function import FunctionModel

    seen = {"tools": None, "returns": []}

    def respond(messages, info):
        seen["tools"] = info.function_tools
        # Tool results and retry prompts both answer one scripted call.
        seen["returns"] = [
            part.content if isinstance(part, ToolReturnPart) else str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, (ToolReturnPart, RetryPromptPart))
        ]
        step = len(seen["returns"])
        if step < len(steps):
            name, args = steps[step]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(respond), seen


@unittest.skipUnless(HAS_PYDANTIC_AI, "uv sync --group agents")
class AnalystTests(unittest.TestCase):
    def test_notebook_state_permissions_and_tool_description(self):
        from agents import analyst

        model, seen = scripted(
            [
                (
                    "run_python",
                    {
                        "code": "import csv\nrows = list(csv.DictReader(open(DATA)))\nprint(len(rows))"
                    },
                ),
                ("run_python", {"code": "print(sorted({r['region'] for r in rows}))"}),
                (
                    "run_python",
                    {"code": "open(OUT + '/report.md', 'w').write('# ok')\nprint('written')"},
                ),
                ("run_python", {"code": "print(open(ENV).read())"}),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            from agents.common import make_sales

            make_sales(root)
            agent = analyst.build_agent(root, model)
            setup = (
                f"DATA = {str(root / 'data' / 'sales.csv')!r}\n"
                f"OUT = {str(root / 'out')!r}\nENV = {str(root / '.env')!r}"
            )
            # The scripted model needs the paths the real model reads from its instructions.
            agent.run_sync(
                f"Run this first:\n{setup}", model=scripted([("run_python", {"code": setup})])[0]
            )
            result = agent.run_sync(analyst.QUESTION)
            self.assertEqual(result.output, "done")
            self.assertEqual(
                seen["returns"][:3], ["72\n", "['east', 'north', 'south', 'west']\n", "written\n"]
            )
            self.assertTrue(
                seen["returns"][3].startswith("PermissionError: [Errno 13]"), seen["returns"][3]
            )
            self.assertNotIn("hunter2", seen["returns"][3])
            self.assertEqual((root / "out" / "report.md").read_text(), "# ok")
            tool = seen["tools"][0]
            self.assertEqual(tool.name, "run_python")
            self.assertIn("persist between calls", tool.description)
            self.assertIn("Everything else is denied", tool.description)


@unittest.skipUnless(HAS_PYDANTIC_AI, "uv sync --group agents")
class CoderTests(unittest.TestCase):
    def test_fix_and_run_tests_in_the_sandbox(self):
        from agents import coder

        fixed = coder.PROJECT["invoice.py"].replace(
            "line['qty'] + line['price']", "line['qty'] * line['price']"
        )
        model, seen = scripted(
            [
                ("run_tests", {}),
                ("write_file", {"path": "invoice.py", "content": fixed}),
                ("write_file", {"path": "../escape.py", "content": "x"}),
                ("run_tests", {}),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            repo, result = coder.run(Path(temporary), model=model)
            self.assertTrue(seen["returns"][0].startswith("exit 1"), seen["returns"][0])
            self.assertIn("FAILED", seen["returns"][0])
            self.assertFalse((Path(temporary) / "escape.py").exists())
            self.assertIn("outside the repository", str(seen["returns"][2]))
            self.assertTrue(seen["returns"][3].startswith("exit 0"), seen["returns"][3])
            self.assertIn("Ran 2 tests", seen["returns"][3])
            self.assertIn("*", (repo / "invoice.py").read_text())


def scripted_strands(steps):
    """A Strands ``Model`` that replays fixed events, so the agent loop runs with no model.

    ``steps`` is a list of event lists, consumed one per ``stream()`` call. This
    drives the real event loop: Strands parses the tool spec from
    ``pynox.tool()``, runs the tool, and feeds the result back as a
    ``toolResult`` content block for the next step to see.
    """

    from strands.models import Model

    def tool_use(tool_id, name, arguments):
        return [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_id, "name": name}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": _usage()},
        ]

    def say(text):
        return [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"start": {}}},
            {"contentBlockDelta": {"delta": {"text": text}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": _usage()},
        ]

    class ScriptedModel(Model):
        def __init__(self):
            self.pending = list(steps)
            self.specs = []
            self.tool_results = []

        def update_config(self, **model_config):
            pass

        def get_config(self):
            return {}

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            self.specs = tool_specs or []
            for message in messages:
                for block in message.get("content", []):
                    if "toolResult" in block:
                        self.tool_results.append(block["toolResult"])
            for event in self.pending.pop(0):
                yield event

        async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
            raise NotImplementedError("the scripted provider only replays events")

    model = ScriptedModel()
    model.tool_use = tool_use
    model.say = say
    return model


def _usage():
    return {
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 0},
    }


@unittest.skipUnless(HAS_STRANDS, "uv sync --group agents")
class SupportTests(unittest.TestCase):
    def test_strands_tool_spec_and_host_functions_under_store_rules(self):
        from agents import support
        from agents.common import make_orders

        with tempfile.TemporaryDirectory() as temporary:
            store = support.Store(make_orders(Path(temporary)))
            from strands.models import BedrockModel

            # Never invoked: the test calls the tool directly, so no request reaches Bedrock.
            agent = support.build_agent(
                store, BedrockModel(model_id="unused", region_name="eu-west-2")
            )
            spec = agent.tool_registry.get_all_tools_config()["run_python"]
            # Strands sends only the text before "Args:"; the functions must be in it.
            self.assertIn("issue_refund(order_id: str, amount: float) -> str", spec["description"])
            self.assertIn("find_orders(customer: str) -> list[dict]", spec["description"])
            code = (
                "from pynox_host import find_orders, issue_refund, HostError\n"
                "for order in find_orders('ana@example.com'):\n"
                "    if order['status'] == 'damaged':\n"
                "        try:\n"
                "            print(issue_refund(order['id'], order['total']))\n"
                "        except HostError as error:\n"
                "            print(error)\n"
            )
            output = agent.tool.run_python(code=code)
            text = "".join(c.get("text", "") for c in output["content"])
            self.assertIn("refunded 24.90 for A-102", text)
            self.assertIn("ticket opened for A-101", text)
            self.assertEqual(store.refunds, [("A-102", 24.9)])
            self.assertEqual(store.tickets, ["A-101"])
            bypass = agent.tool.run_python(
                code=f"import sqlite3\nsqlite3.connect({str(store.database)!r}).execute('select 1')"
            )
            self.assertIn(
                "unable to open database file",
                "".join(c.get("text", "") for c in bypass["content"]),
            )

    def test_strands_agent_loop_runs_the_tool_and_feeds_the_result_back(self):
        """The full loop with a scripted model: no network, no Bedrock, real tool."""
        from agents import support
        from agents.common import make_orders

        code = (
            "from pynox_host import find_orders, issue_refund, HostError\n"
            "for order in find_orders('ana@example.com'):\n"
            "    if order['status'] == 'damaged':\n"
            "        try:\n"
            "            print(issue_refund(order['id'], order['total']))\n"
            "        except HostError as error:\n"
            "            print(error)\n"
        )
        model = scripted_strands([])
        model.pending = [
            model.tool_use("call-1", "run_python", {"code": code}),
            model.say("I refunded the damaged orders."),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            store = support.Store(make_orders(Path(temporary)))
            agent = support.build_agent(store, model)

            result = asyncio.run(agent.invoke_async("Ana needs help with a delivery."))

            self.assertIn("refunded", str(result))
            # Strands built the spec from pynox.tool(), host functions included.
            self.assertEqual([spec["name"] for spec in model.specs], ["run_python"])
            self.assertIn(
                "find_orders(customer: str) -> list[dict]",
                model.specs[0]["description"],
            )
            # The tool really ran in the pool and its output came back to the model.
            self.assertEqual(len(model.tool_results), 1, model.tool_results)
            result_block = model.tool_results[0]
            self.assertEqual(result_block["status"], "success", result_block)
            text = "".join(block.get("text", "") for block in result_block["content"])
            self.assertIn("refunded 24.90 for A-102", text)
            self.assertIn("ticket opened for A-101", text)
            self.assertEqual(store.refunds, [("A-102", 24.9)])
            self.assertEqual(store.tickets, ["A-101"])
