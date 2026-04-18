"""Shared types for sync and async process wrappers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    # Avoid circular import at runtime; only used for type hints.
    pass

StreamName = Literal["stdout", "stderr"]


class ProcessError(Exception):
    """Base class for procstream errors."""


class TimeoutExpired(ProcessError):
    """A process exceeded its configured timeout and was killed.

    Attributes:
        pid: Process id of the killed child.
        timeout: The timeout value that was exceeded, in seconds.
        result: Partial Result with whatever output was captured before kill.
    """

    def __init__(self, pid: int, timeout: float, result: Result) -> None:
        super().__init__(
            f"Process {pid} did not finish within {timeout}s and was killed."
        )
        self.pid = pid
        self.timeout = timeout
        self.result = result


class CalledProcessError(ProcessError):
    """Raised when `check=True` and the process exits non-zero.

    Modeled after `subprocess.CalledProcessError`. Exposes the full `Result`
    so callers can still inspect captured output.
    """

    def __init__(self, returncode: int, cmd: object, result: Result) -> None:
        super().__init__(
            f"Command {cmd!r} failed with exit code {returncode}."
        )
        self.returncode = returncode
        self.cmd = cmd
        self.result = result


@dataclass(frozen=True)
class Line:
    """A single line of output from the child process.

    The trailing newline is stripped. If the child emits a final partial
    line without a newline before EOF, that partial line is still yielded.
    """

    text: str
    stream: StreamName

    @property
    def is_stderr(self) -> bool:
        return self.stream == "stderr"

    def __str__(self) -> str:
        return self.text


@dataclass
class Result:
    """Final result of a completed process."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        """Stdout followed by stderr. For real interleaved order, iterate `stream()`."""
        if not self.stderr:
            return self.stdout
        if not self.stdout:
            return self.stderr
        return self.stdout + self.stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def raise_for_returncode(self, cmd: object = "<cmd>") -> None:
        """Raise `CalledProcessError` if this result represents a non-zero exit."""
        if not self.ok:
            raise CalledProcessError(self.returncode, cmd, self)
