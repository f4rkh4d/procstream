"""procstream — stream subprocess output with timeouts and tree-kill."""

from ._core import (
    Line,
    Process,
    ProcessError,
    Result,
    TimeoutExpired,
    run,
)

__all__ = [
    "Line",
    "Process",
    "ProcessError",
    "Result",
    "TimeoutExpired",
    "run",
]

__version__ = "0.1.0"
