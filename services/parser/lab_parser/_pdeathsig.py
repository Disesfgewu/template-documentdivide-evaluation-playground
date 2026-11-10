"""The one implementation of "die with the parent" for the parser children (Refs #7).

Loaded by path from ``_isolation_child.py`` and ``_unstructured_child.py`` (they run under
``python -I``, which puts neither the script directory nor the package on ``sys.path``).
"""
from __future__ import annotations

import os


def die_with_parent() -> None:
    """Ask the kernel to SIGKILL this process when its parent dies (Linux PR_SET_PDEATHSIG).

    The child runs in its own session so a cancelled parent can kill its group, but a parent that is
    SIGKILLed or ``os._exit``ed never gets to; without this the parser would keep running (up to its
    CPU/wall limits) with nobody to read its result.

    ``LAB_PARENT_PID`` (set by ``isolation._run_child`` to the spawning process) is the parent the
    child must still have. Comparing against it closes the window in which the parent died before the
    child's first ``getppid()``: reading ``getppid()`` only after the parent was gone would have
    recorded the new parent (pid 1 or a subreaper) as the expected one.
    """
    import ctypes
    import signal

    try:
        expected = os.environ.get("LAB_PARENT_PID")
        parent = int(expected) if expected and expected.isdigit() else os.getppid()
        if os.getppid() != parent:
            os._exit(1)  # already orphaned
        if ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            return
        if os.getppid() != parent:  # the parent died before the call took effect
            os._exit(1)
    except Exception:
        return  # best effort: the limits and the parent's own cancel path still apply
