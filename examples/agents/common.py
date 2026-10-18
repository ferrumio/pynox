"""Shared setup for the agent examples: which model to use, and sample data.

The examples call Claude through Amazon Bedrock by default, so they work with
plain AWS credentials. Override with environment variables:

    PYNOX_AGENT_MODEL           Bedrock model or inference profile ID
                                (default: eu.anthropic.claude-opus-5-5)
    PYNOX_AGENT_FALLBACK_MODEL  Model to retry with when the first one declines
                                (default: eu.anthropic.claude-opus-4-8)
    AWS_REGION                  Bedrock region (default: eu-west-2)
"""

from __future__ import annotations

import csv
import os
import random
import sqlite3
from pathlib import Path

DEFAULT_MODEL = "eu.anthropic.claude-opus-5-5"
DEFAULT_FALLBACK_MODEL = "eu.anthropic.claude-opus-4-8"


def model_id() -> str:
    return os.environ.get("PYNOX_AGENT_MODEL", DEFAULT_MODEL)


def fallback_model_id() -> str:
    """Safety classifiers occasionally decline a benign request; retry on this model.

    Bedrock has no server-side fallback, so the examples retry client-side.
    """
    return os.environ.get("PYNOX_AGENT_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)


def region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-2"


def make_sales(root: Path) -> Path:
    """Two quarters of sales for four regions; south grows fastest."""
    rng = random.Random(7)
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    path = data / "sales.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "region", "product", "units", "revenue"])
        for quarter, months in (("Q1", (1, 2, 3)), ("Q2", (4, 5, 6))):
            for region_name, trend in (
                ("north", 1.05),
                ("south", 1.31),
                ("east", 0.92),
                ("west", 1.12),
            ):
                for month in months:
                    for product in ("widget", "gadget", "gizmo"):
                        units = rng.randint(40, 120)
                        price = {"widget": 12.5, "gadget": 30.0, "gizmo": 7.25}[product]
                        factor = trend if quarter == "Q2" else 1.0
                        writer.writerow(
                            [
                                f"2026-{month:02d}-15",
                                region_name,
                                product,
                                round(units * factor),
                                f"{units * price * factor:.2f}",
                            ]
                        )
    (root / "out").mkdir(exist_ok=True)
    (root / ".env").write_text("WAREHOUSE_PASSWORD=hunter2\n")
    return path


def make_orders(root: Path) -> Path:
    database = root / "orders.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "create table if not exists orders (id text, customer text, item text, total real, status text)"
        )
        db.execute("delete from orders")
        db.executemany(
            "insert into orders values (?, ?, ?, ?, ?)",
            [
                ("A-100", "ana@example.com", "desk lamp", 42.0, "delivered"),
                ("A-101", "ana@example.com", "office chair", 310.0, "damaged"),
                ("A-102", "ana@example.com", "usb hub", 24.9, "damaged"),
                ("B-200", "bo@example.com", "monitor arm", 89.0, "delivered"),
            ],
        )
    return database
