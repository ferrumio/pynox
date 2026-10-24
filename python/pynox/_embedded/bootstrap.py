"""Trusted worker definitions; _pynox_run needs an active sandbox.

This module deliberately imports only ``os`` and ``sys``, which CPython has
already initialized. Every additional import here is paid by every task.
"""

import os
import sys


def _pynox_env(environment):
    os.environ.clear()
    os.environ.update(environment)


def _pynox_prepare(script, argv0, args, package_dirs, site_packages, source, module):
    # Avoid importing site: .pth and sitecustomize must not run automatically.
    sys.path.extend(path for path in site_packages if path not in sys.path)
    sys.path.extend(package_dirs)
    if module is not None:
        # `python -m` resolves modules from the current directory first.
        sys.path.insert(0, os.getcwd())
    elif source is None:
        sys.path.insert(0, os.path.dirname(script))
    sys.argv = [argv0, *args]
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # Pool children inherit streams initialized against /dev/null.
            # Recheck the task's descriptors after the fork, as CPython does
            # at startup: stdout follows isatty(), stderr is line-buffered.
            stream.reconfigure(
                line_buffering=stream is sys.stderr or stream.isatty(), write_through=False
            )


def _pynox_main(script, source):
    main = type(sys)("__main__")
    if source is None:
        main.__file__ = script
    main.__package__ = None
    sys.modules["__main__"] = main
    return main


def _pynox_outcome(exc, script, capture):
    """Exit status and structured error for an exception raised by user code."""
    if isinstance(exc, SystemExit):
        if exc.code is None:
            return 0, None
        if isinstance(exc.code, int):
            return exc.code & 255, None
        print(str(exc.code), file=sys.stderr)
        return 1, None
    if not capture:
        # Passthrough mode behaves like python: the traceback goes to stderr.
        import traceback

        # Skip the launcher's own frame, as python hides its launcher.
        frames = exc.__traceback__
        traceback.print_exception(type(exc), exc, frames.tb_next if frames else frames)
    line = None
    filename = None
    script_line = None
    tb = exc.__traceback__
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        line = tb.tb_lineno
        if filename == script:
            script_line = line
        tb = tb.tb_next
    if script_line is not None:
        filename, line = script, script_line
    if isinstance(exc, SyntaxError):
        line = exc.lineno
        filename = exc.filename
    hint = None
    if isinstance(exc, PermissionError):
        hint = (
            "For files, grant the specific path with --allow-read/--allow-write. "
            "Use --allow-network for IPv4/IPv6. Subprocesses and host Unix sockets are denied."
        )
    elif isinstance(exc, MemoryError):
        hint = "Reduce allocations or explicitly increase --memory."
    return 1, {
        "type": type(exc).__name__,
        "message": str(exc)[:4096],
        "file": filename,
        "line": line,
        "path": getattr(exc, "filename", None),
        "hint": hint,
    }


def _pynox_host(names):
    """Install `pynox_host`, exposing exactly the functions the host declared.

    A call writes one JSON line on the report descriptor and blocks until the
    host answers on descriptor 4 with `<length>\\n<json>`.
    """
    if not names:
        return
    import _thread

    lock = _thread.allocate_lock()
    module = type(sys)("pynox_host")
    module.__doc__ = "Functions provided by the application that runs this code."

    class HostError(Exception):
        """The host refused or failed a call."""

    HostError.__module__ = "pynox_host"
    module.HostError = HostError

    def read_exactly(size):
        data = bytearray()
        while len(data) < size:
            chunk = os.read(4, size - len(data))
            if not chunk:
                raise HostError("The host closed the call channel")
            data += chunk
        return bytes(data)

    def make(name):
        def call(*args, **kwargs):
            import json

            frame = json.dumps(
                {"pynox_call": {"function": name, "args": args, "kwargs": kwargs}},
                default=str,
            ).encode()
            if len(frame) > 1_000_000:
                raise HostError("Host call arguments exceed 1 MB")
            with lock:
                view = memoryview(frame + b"\n")
                while view:
                    view = view[os.write(3, view) :]
                header = b""
                while not header.endswith(b"\n"):
                    header += read_exactly(1)
                reply = json.loads(read_exactly(int(header)))
            if reply.get("error") is not None:
                raise HostError(reply["error"])
            return reply.get("result")

        call.__name__ = call.__qualname__ = name
        call.__module__ = "pynox_host"
        return call

    for name in names:
        setattr(module, name, make(name))
    module.__all__ = list(names)
    sys.modules["pynox_host"] = module


def _pynox_run(
    script, argv0, args, package_dirs, site_packages, source, module, capture, functions
):
    _pynox_prepare(script, argv0, args, package_dirs, site_packages, source, module)
    _pynox_host(functions)
    exit_code, error = 0, None
    try:
        if module is not None:
            import runpy

            runpy.run_module(module, run_name="__main__", alter_sys=True)
        else:
            main = _pynox_main(script, source)
            if source is None:
                # compile(bytes) respects Python source encoding declarations.
                with open(script, "rb") as file:
                    source = file.read()
            exec(compile(source, script, "exec"), main.__dict__)
    except BaseException as exc:
        exit_code, error = _pynox_outcome(exc, script, capture)
    sys.stdout.flush()
    sys.stderr.flush()
    # The launcher serializes this report on the diagnostic descriptor. It is
    # not a tamper-proof audit log.
    return exit_code, error


_session = {}


def _pynox_session_start(script, argv0, args, package_dirs, site_packages, capture, functions):
    """Prepare one `__main__` that every step of a session shares."""
    _pynox_prepare(script, argv0, args, package_dirs, site_packages, "", None)
    _pynox_host(functions)
    _session["main"] = _pynox_main(script, "")
    _session["capture"] = capture


def _pynox_session_step(source, name):
    """Run one step in the session's `__main__`; state persists, errors do not end it."""
    exit_code, error = 0, None
    try:
        exec(compile(source, name, "exec"), _session["main"].__dict__)
    except BaseException as exc:
        exit_code, error = _pynox_outcome(exc, name, _session["capture"])
    sys.stdout.flush()
    sys.stderr.flush()
    return exit_code, error
