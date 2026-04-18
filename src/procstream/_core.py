"""Core implementation of procstream.

Stream a subprocess's stdout and stderr as lines arrive, with timeouts and
tree-kill. Stdlib only, no external dependencies.

Threading model
---------------
Two reader threads drain the child's stdout and stderr pipes into a single
queue, tagging each chunk with its stream of origin. The main thread reads
from this queue in `Process.stream()`, which preserves arrival order across
streams. This avoids the Popen deadlock you get from naive blocking reads
on two pipes.

Process groups
--------------
`Popen` is started in a new process group (POSIX: `start_new_session=True`;
Windows: `CREATE_NEW_PROCESS_GROUP`). That lets us kill the whole tree
when the user asks to cancel or a timeout fires — just signaling the direct
child would leak any grandchildren it spawned.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import (
    IO,
    Any,
    Callable,
    Iterator,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    Union,
)

_IS_WINDOWS = sys.platform == "win32"

StreamName = Literal["stdout", "stderr"]
LineCallback = Callable[[str], None]


class ProcessError(Exception):
    """Base class for procstream errors."""


class TimeoutExpired(ProcessError):
    """Raised when a process exceeds its configured timeout.

    Attributes:
        pid: process id of the killed child.
        timeout: the timeout value that was exceeded.
        result: partial Result with whatever output was captured before kill.
    """

    def __init__(self, pid: int, timeout: float, result: "Result") -> None:
        super().__init__(
            f"Process {pid} did not finish within {timeout}s and was killed."
        )
        self.pid = pid
        self.timeout = timeout
        self.result = result


@dataclass(frozen=True)
class Line:
    """A single line of output from the child process.

    Newlines are stripped from `text`; trailing partial output (no newline
    before EOF) is still yielded as its own Line.
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
        """Stdout followed by stderr. For interleaved order, use Process.stream()."""
        if not self.stderr:
            return self.stdout
        if not self.stdout:
            return self.stderr
        return self.stdout + self.stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0


_EOF = object()  # sentinel on the internal queue


class Process:
    """A running or finished subprocess.

    Usually produced by `run()`. Consume output via `stream()` or `wait()`.

    Once started, the child runs until it exits naturally, is killed via
    `terminate_tree()` / `kill_tree()`, or the timeout fires. All three paths
    clean up the reader threads and the process group.
    """

    def __init__(
        self,
        cmd: Union[str, Sequence[str]],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        shell: bool = False,
        timeout: Optional[float] = None,
        encoding: str = "utf-8",
        errors: str = "replace",
        on_stdout: Optional[LineCallback] = None,
        on_stderr: Optional[LineCallback] = None,
        bufsize: int = 1,
    ) -> None:
        self._cmd = cmd
        self._timeout = timeout
        self._encoding = encoding
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr

        popen_kwargs: dict = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            shell=shell,
            text=True,
            encoding=encoding,
            errors=errors,
            bufsize=bufsize,
        )
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        self._popen = subprocess.Popen(cmd, **popen_kwargs)

        self._q: "queue.Queue[Union[Line, object]]" = queue.Queue()
        self._threads_done = 0
        self._threads_total = 2
        self._lock = threading.Lock()
        self._consumed = False
        self._buf_stdout: List[str] = []
        self._buf_stderr: List[str] = []
        self._timed_out = False
        self._deadline: Optional[float] = (
            time.monotonic() + timeout if timeout is not None else None
        )

        self._t_out = threading.Thread(
            target=self._drain,
            args=(self._popen.stdout, "stdout", self._buf_stdout, self._on_stdout),
            name=f"procstream-stdout-{self.pid}",
            daemon=True,
        )
        self._t_err = threading.Thread(
            target=self._drain,
            args=(self._popen.stderr, "stderr", self._buf_stderr, self._on_stderr),
            name=f"procstream-stderr-{self.pid}",
            daemon=True,
        )
        self._t_out.start()
        self._t_err.start()

        if self._deadline is not None:
            self._t_timeout = threading.Thread(
                target=self._watch_timeout,
                name=f"procstream-timeout-{self.pid}",
                daemon=True,
            )
            self._t_timeout.start()
        else:
            self._t_timeout = None

    # ---- public ----

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._popen.returncode

    @property
    def running(self) -> bool:
        return self._popen.poll() is None

    def stream(self) -> Iterator[Line]:
        """Yield lines in arrival order until the child exits.

        Stripping: `\\n` / `\\r\\n` are removed. A trailing partial line (no
        newline before EOF) is yielded as its own Line. Each Line is also
        buffered internally so that `wait().stdout` / `.stderr` are populated
        regardless of whether you consume the iterator.
        """
        with self._lock:
            if self._consumed:
                raise ProcessError("stream() already consumed; call wait() instead")
            self._consumed = True

        exhausted_normally = False
        try:
            done = 0
            while done < self._threads_total:
                item = self._q.get()
                if item is _EOF:
                    done += 1
                    continue
                yield item  # type: ignore[misc]
            exhausted_normally = True
        finally:
            self._popen.wait()
        # Only raise after the generator fully ran out; if the caller closed
        # us early (break, exception), propagating TimeoutExpired from here
        # would fight GeneratorExit and produce an "unraisable" warning.
        if exhausted_normally and self._timed_out:
            raise TimeoutExpired(
                self.pid,
                self._timeout,  # type: ignore[arg-type]
                self._build_result(),
            )

    def wait(self) -> Result:
        """Wait for completion and return a Result.

        If `stream()` hasn't been consumed, output is drained internally
        (callbacks still fire).
        """
        if not self._consumed:
            with self._lock:
                self._consumed = True
            done = 0
            while done < self._threads_total:
                item = self._q.get()
                if item is _EOF:
                    done += 1
        self._popen.wait()
        if self._timed_out:
            raise TimeoutExpired(
                self.pid,
                self._timeout,  # type: ignore[arg-type]
                self._build_result(),
            )
        return self._build_result()

    def terminate_tree(self, grace: float = 5.0) -> int:
        """Send SIGTERM to the whole process group, wait up to `grace` seconds,
        then SIGKILL if still alive. Returns the final returncode.
        """
        if self._popen.poll() is not None:
            return self._popen.returncode  # type: ignore[return-value]
        self._signal_group(_sigterm())
        try:
            return self._popen.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.kill_tree()
            return self._popen.wait()

    def kill_tree(self) -> int:
        """SIGKILL the whole process group immediately. Returns returncode."""
        if self._popen.poll() is None:
            self._signal_group(_sigkill())
        return self._popen.wait()

    # context manager: ensures the tree dies if the caller bails out

    def __enter__(self) -> "Process":
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._popen.poll() is None:
            self.terminate_tree(grace=1.0)

    # ---- internal ----

    def _drain(
        self,
        pipe: Optional[IO[str]],
        stream_name: StreamName,
        buf: List[str],
        callback: Optional[LineCallback],
    ) -> None:
        try:
            if pipe is None:
                return
            for raw in pipe:
                line_text = raw.rstrip("\r\n") if raw.endswith("\n") else raw
                buf.append(line_text)
                if callback is not None:
                    try:
                        callback(line_text)
                    except Exception:  # noqa: BLE001 — user callback isolation
                        # Swallow to keep the drain alive; user bugs shouldn't
                        # wedge the subprocess reader.
                        pass
                self._q.put(Line(line_text, stream_name))
        finally:
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:  # noqa: BLE001
                pass
            self._q.put(_EOF)

    def _watch_timeout(self) -> None:
        assert self._deadline is not None
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                if self._popen.poll() is None:
                    self._timed_out = True
                    # Be polite first, then forceful.
                    self._signal_group(_sigterm())
                    try:
                        self._popen.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self._signal_group(_sigkill())
                return
            if self._popen.poll() is not None:
                return
            time.sleep(min(remaining, 0.25))

    def _signal_group(self, sig: int) -> None:
        pid = self._popen.pid
        try:
            if _IS_WINDOWS:
                # CTRL_BREAK_EVENT maps to SIGTERM-like; otherwise force-kill
                # the whole tree via taskkill.
                if sig == _sigterm():
                    try:
                        self._popen.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                        return
                    except Exception:  # noqa: BLE001
                        pass
                # Fall through to forceful tree kill.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                try:
                    os.killpg(os.getpgid(pid), sig)
                except ProcessLookupError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _build_result(self) -> Result:
        return Result(
            returncode=self._popen.returncode if self._popen.returncode is not None else -1,
            stdout="\n".join(self._buf_stdout),
            stderr="\n".join(self._buf_stderr),
        )


def run(
    cmd: Union[str, Sequence[str]],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    shell: bool = False,
    timeout: Optional[float] = None,
    encoding: str = "utf-8",
    errors: str = "replace",
    on_stdout: Optional[LineCallback] = None,
    on_stderr: Optional[LineCallback] = None,
) -> Process:
    """Spawn `cmd` and return a `Process`.

    The child starts immediately. Output is captured lazily — iterate
    `stream()` to read lines as they arrive, or call `wait()` to block until
    completion and get a `Result`.

    `cmd` follows subprocess conventions: a list of args (preferred) or a
    single string with `shell=True`.

    Timeouts run in a watcher thread: when exceeded, the whole process group
    gets SIGTERM, then SIGKILL if not exited within 2 seconds. The user sees
    a `TimeoutExpired` on the next `wait()` or when the stream generator
    finishes draining buffered output.
    """
    return Process(
        cmd,
        cwd=cwd,
        env=env,
        shell=shell,
        timeout=timeout,
        encoding=encoding,
        errors=errors,
        on_stdout=on_stdout,
        on_stderr=on_stderr,
    )


def _sigterm() -> int:
    return signal.SIGTERM


def _sigkill() -> int:
    return getattr(signal, "SIGKILL", signal.SIGTERM)
