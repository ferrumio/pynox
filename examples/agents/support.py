"""A support agent (Strands Agents + Claude on Bedrock) that calls your backend.

The model resolves a customer case by writing Python that calls your own
functions, `find_orders` and `issue_refund`. Those functions run in your
process with your database and your rules; the model's code runs in Pynox and
can reach the backend only through them.

    uv run --group agents python -m agents.support      # from examples/, with AWS credentials
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pynox
from strands import Agent, tool
from strands.models import BedrockModel

from agents.common import fallback_model_id, make_orders, model_id, region

SYSTEM = """You are a customer support agent for an online store.
Use the run_python tool to look up orders and issue refunds: inside it,
`from pynox_host import find_orders, issue_refund` gives you the store's API.
Follow the store's rules; if a refund is refused, tell the customer what happens next.
Reply to the customer in two or three sentences."""

REQUEST = "Hi, this is Ana (ana@example.com). Two things in my last delivery arrived broken. Can you help?"


class Store:
    """The application's backend. Rules live here, not in the prompt."""

    REFUND_LIMIT = 200.0

    def __init__(self, database: Path):
        self.database = database
        self.refunds: list[tuple[str, float]] = []
        self.tickets: list[str] = []

    def find_orders(self, customer: str) -> list[dict]:
        """List a customer's orders: id, item, total and status."""
        with sqlite3.connect(self.database) as db:
            rows = db.execute(
                "select id, item, total, status from orders where customer = ?", (customer,)
            ).fetchall()
        return [{"id": i, "item": it, "total": t, "status": s} for i, it, t, s in rows]

    def issue_refund(self, order_id: str, amount: float) -> str:
        """Refund a damaged order. Refunds above 200 open a ticket for a human instead."""
        with sqlite3.connect(self.database) as db:
            row = db.execute(
                "select total, status from orders where id = ?", (order_id,)
            ).fetchone()
        if row is None or row[1] != "damaged":
            raise ValueError(f"{order_id} is not a damaged order")
        if amount > min(row[0], self.REFUND_LIMIT):
            self.tickets.append(order_id)
            raise PermissionError(
                f"refund of {amount:.2f} needs approval; ticket opened for {order_id}"
            )
        self.refunds.append((order_id, amount))
        return f"refunded {amount:.2f} for {order_id}"


def build_agent(store: Store, model=None) -> Agent:
    run_python = tool(
        pynox.tool(functions={"find_orders": store.find_orders, "issue_refund": store.issue_refund})
    )
    if model is None:
        model = BedrockModel(model_id=model_id(), region_name=region())
    return Agent(model=model, system_prompt=SYSTEM, tools=[run_python], callback_handler=None)


def run(root: Path, request: str = REQUEST, model=None):
    store = Store(make_orders(root))
    agent = build_agent(store, model)
    result = agent(request)
    if model is None and result.stop_reason == "content_filtered":
        # The model's safety classifier declined; nothing ran. Retry on the fallback model.
        print(f"[{model_id()} declined; retrying on {fallback_model_id()}]")
        store = Store(make_orders(root))
        agent = build_agent(store, BedrockModel(model_id=fallback_model_id(), region_name=region()))
        result = agent(request)
    return agent, store, result


def print_transcript(agent, store, result) -> None:
    for message in agent.messages:
        for block in message.get("content", []):
            if "toolUse" in block:
                print(f"\n--- model runs Python ---\n{block['toolUse']['input'].get('code', '')}")
            elif "toolResult" in block:
                text = "".join(c.get("text", "") for c in block["toolResult"]["content"])
                print(f"--- Pynox returns ---\n{text.rstrip()}")
    print(f"\n--- reply to the customer ---\n{str(result).strip()}")
    print(f"\n--- backend state ---\nrefunds: {store.refunds}\ntickets: {store.tickets}")
    stats = pynox.default_pool().stats()
    print(
        f"\n--- cost of the sandbox ---\n{stats['jobs']} Python runs, "
        f"{stats['duration_ms_total']} ms inside Pynox in total"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pynox-support-") as temporary:
        agent, store, result = run(Path(temporary), " ".join(sys.argv[1:]) or REQUEST)
        print_transcript(agent, store, result)
