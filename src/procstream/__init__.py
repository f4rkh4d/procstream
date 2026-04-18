"""procstream — stream subprocess output with timeouts, tree-kill, and sane defaults.

Sync::

    from procstream import run
    for line in run(["pytest", "-q"]).stream():
        print("!!" if line.is_stderr else "  ", line.text)

Async::

    import asyncio
    from procstream import arun

    async def main():
        proc = await arun(["pytest", "-q"])
        async for line in proc.stream():
            print(line.text)

    asyncio.run(main())
"""

from ._async import AsyncProcess, arun
from ._sync import Process, run
from ._types import (
    CalledProcessError,
    Line,
    ProcessError,
    Result,
    TimeoutExpired,
)

__all__ = [
    "AsyncProcess",
    "CalledProcessError",
    "Line",
    "Process",
    "ProcessError",
    "Result",
    "TimeoutExpired",
    "arun",
    "run",
]

__version__ = "0.2.0"
