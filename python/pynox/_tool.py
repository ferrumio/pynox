"""`pynox.run` and `pynox.tool`: the two calls most applications need."""

from __future__ import annotations

import atexit
import threading
from typing import Any, Callable

from pynox._client import Pool, Result

_default: Pool | None = None
_default_lock = threading.Lock()


def default_pool() -> Pool:
    """The process-wide pool behind ``pynox.run`` and ``pynox.tool``.

    It starts on first use with the pool's own defaults and the ``pynox.toml``
    the binary discovers (``$PYNOX_CONFIG``, then the current directory), and
    closes at interpreter exit. Build a ``Pool`` yourself for other settings.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = Pool()
            atexit.register(_default.close)
        return _default


def run(code: str, **options: Any) -> Result:
    """Run ``code`` sandboxed on the default pool. Options are ``Pool.run``'s."""
    return default_pool().run(code, **options)


def tool(profile: str | None = None, **options: Any) -> Callable[[str], str]:
    """A ``run_python(code: str) -> str`` function ready to register as an agent tool.

    Every agent framework accepts a typed, documented Python function, so the
    same object works with Pydantic AI (``tools=[pynox.tool()]``), Strands
    (``tool(pynox.tool())``), smolagents (``tool(pynox.tool())``) and Monty
    (``external_lookup={"run_python": pynox.tool()}``).

    ``profile`` names a permission set from ``pynox.toml``; keyword options are
    ``Pool.run``'s and apply to every call. ``session=True`` keeps one isolated
    process for the tool, so variables survive between calls. ``functions``
    maps names to host callables the code can use via ``pynox_host``, listed
    with their signatures in the docstring the model reads. ``name`` and
    ``description`` change what the model sees. The model only ever supplies
    ``code``.
    """
    return make_tool(default_pool(), profile=profile, **options)


def make_tool(
    pool: Pool,
    *,
    profile: str | None = None,
    name: str = "run_python",
    description: str | None = None,
    session: bool | str = False,
    **options: Any,
) -> Callable[[str], str]:
    if session:
        # One persistent process: variables defined in one call are there in the next.
        state = pool.session(None if session is True else session, profile=profile, **options)

        def run_python(code: str) -> str:
            return state.run(code).text

    else:

        def run_python(code: str) -> str:
            return pool.run(code, profile=profile, **options).text

    run_python.__name__ = name
    run_python.__qualname__ = name
    run_python.__annotations__ = {"code": str, "return": str}
    run_python.__doc__ = description or _describe(profile, {**options, "session": bool(session)})
    return run_python


def _signature(function: Callable[..., Any]) -> str:
    import inspect

    try:
        return str(inspect.signature(function, eval_str=True))
    except (NameError, TypeError, ValueError):
        try:
            return str(inspect.signature(function)).replace("'", "")
        except (TypeError, ValueError):
            return "(...)"


def _describe(profile: str | None, options: dict[str, Any]) -> str:
    # Frameworks that follow Google-style docstrings (Strands, smolagents) send only
    # the text before "Args:" as the tool description, so everything the model needs
    # goes first and the Args section comes last.
    lines = ["Run Python code in an isolated sandbox and return what it printed.", ""]
    grants = []
    if options.get("allow_read"):
        grants.append("read " + ", ".join(str(p) for p in options["allow_read"]))
    if options.get("allow_write"):
        grants.append("write " + ", ".join(str(p) for p in options["allow_write"]))
    if options.get("allow_network"):
        grants.append("use the network")
    if profile:
        grants.append(f"use the permissions of profile {profile!r}")
    if grants:
        lines.append("The code may " + "; ".join(grants) + ". Everything else is denied.")
    else:
        lines.append(
            "The code has no file, network or environment access beyond the standard library."
        )
    if options.get("session"):
        lines.append("Variables and imports persist between calls, like a notebook.")
    functions = options.get("functions") or {}
    if functions:
        lines += ["", "The code can call these host functions after `from pynox_host import ...`:"]
        for fname, function in sorted(functions.items()):
            summary = (function.__doc__ or "").strip().splitlines()
            lines.append(
                f"- {fname}{_signature(function)}" + (f": {summary[0]}" if summary else "")
            )
        lines.append(
            "Arguments and results travel as JSON; a failed call raises pynox_host.HostError."
        )
    lines += [
        "Errors come back as the exception type, message, line and a hint.",
        "",
        "Args:",
        "    code: Python source to execute. Print anything you need back; only",
        "        standard output is returned, so prefer compact summaries.",
    ]
    return "\n".join(lines)
