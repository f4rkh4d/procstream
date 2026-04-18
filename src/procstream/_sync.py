"""Synchronous process wrapper.

Two reader threads drain the child's stdout and stderr into a single queue,
tagging each chunk with its stream of origin. The main thread reads from
the queue in `Process.stream()`, which preserves arrival order across
streams. This avoids the Popen deadlock you get from naive blocking reads
on two pipes.

Process groups
--------------
`Popen` is started in a new process group (POSIX: `start_new_session=True`;
Windows: `CREATE_NEW_PROCESS_GROUP`). That lets us kill the whole tree when
the user cancels or a timeout fires.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import (
    IO,
    Any,
    Callable,
    List,
    Optional,
    Union,
)

from ._types import (
    CalledProcessError,
    Line,
    ProcessError,
    Result,
    StreamName,
    TimeoutExpired,
)

_IS_WINDOWS = sys.platform == "win32"
_EOF = object()  # sentinel on the internal queue

LineCallback = Callable[[str], None]
CommandLike = Union[str, Sequence[Union[str, os.PathLike]]]
StdinLike = Union[str, bytes, IO, None]


def _merge_env(env_add: Optional[Mapping[str, str]]) -> Optional[dict]:
    """Inherit os.environ and overlay `env_add`. If `env_add` is None, return None
    so Popen inherits os.environ directly (cheapest path)."""
    if env_add is None:
        return None
    merged = os.environ.copy()
    merged.update(env_add)
    return merged


class Process:
    """A running or finished subprocess (sync)."""

    def __init__(
        self,
        cmd: CommandLike,
        *,
        cwd: Union[str, Path, None] = None,
        env: Optional[Mapping[str, str]] = None,
        env_add: Optional[Mapping[str, str]] = None,
        shell: bool = False,
        timeout: Optional[float] = None,
        encoding: str = "utf-8",
        errors: str = "replace",
        on_stdout: Optional[LineCallback] = None,
        on_stderr: Optional[LineCallback] = None,
        stdin: StdinLike = None,
        merge_stderr: bool = False,
        check: bool = False,
        bufsize: int = 1,
    ) -> None:
        if env is not None and env_add is not None:
            raise ValueError("pass either env or env_add, not both")

        self._cmd = cmd
        self._timeout = timeout
        self._encoding = encoding
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._check = check
        self._merge_stderr = merge_stderr

        effective_env: Optional[Mapping[str, str]]
        if env is not None:
            effective_env = env
        elif env_add is not None:
            effective_env = _merge_env(env_add)
        else:
            effective_env = None

        stdin_arg, stdin_payload = _prepare_stdin(stdin)

        popen_kwargs: dict = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            stdin=stdin_arg,
            cwd=str(cwd) if isinstance(cwd, Path) else cwd,
            env=effective_env,
            shell=shell,
            text=True,
            encoding=encoding,
            errors=errors,
            bufsize=bufsize,
        )
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            popen_kwargs["start_new_session"] = True

        self._popen = subprocess.Popen(cmd, **popen_kwargs)

        # Feed stdin if a payload was supplied.
        if stdin_payload is not None:
            assert self._popen.stdin is not None
            try:
                self._popen.stdin.write(stdin_payload)
            finally:
                try:
                    self._popen.stdin.close()
                except Exception:
                    pass

        self._q: queue.Queue[Union[Line, object]] = queue.Queue()
        self._threads_total = 1 if merge_stderr else 2
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
        self._t_out.start()

        if not merge_stderr:
            self._t_err = threading.Thread(
                target=self._drain,
                args=(self._popen.stderr, "stderr", self._buf_stderr, self._on_stderr),
                name=f"procstream-stderr-{self.pid}",
                daemon=True,
            )
            self._t_err.start()

        if self._deadline is not None:
            self._t_timeout: Optional[threading.Thread] = threading.Thread(
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

        Newlines are stripped. A trailing partial line (no newline before
        EOF) is yielded as its own Line. Each yielded Line is also buffered
        internally, so `wait()` returns populated stdout/stderr even if you
        consume the iterator.
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
                yield item  # type: ignore[misc,unused-ignore]
            exhausted_normally = True
        finally:
            self._popen.wait()

        # Only raise after the generator fully ran out; raising from `finally`
        # during GeneratorExit would fight the exit and produce an
        # "unraisable" warning on some interpreters.
        if exhausted_normally:
            self._check_post_exit()

    def wait(self) -> Result:
        """Wait for completion and return a `Result`.

        If `stream()` hasn't been consumed, output is drained internally
        (callbacks still fire, output is still buffered into the Result).
        Raises `TimeoutExpired` if the process was killed by the timeout,
        or `CalledProcessError` if `check=True` and the exit was non-zero.
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
        self._check_post_exit()
        return self._build_result()

    def terminate_tree(self, grace: float = 5.0) -> int:
        """SIGTERM the whole process group, wait up to `grace` seconds, then
        SIGKILL if still alive. Returns the final returncode."""
        if self._popen.poll() is not None:
            return self._popen.returncode  # type: ignore[return-value,unused-ignore]
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

    # Context manager: ensure the tree dies if the caller bails out.

    def __enter__(self) -> Process:
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
                    except Exception:
                        # Don't let a buggy callback wedge the reader.
                        pass
                self._q.put(Line(line_text, stream_name))
        finally:
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass
            self._q.put(_EOF)

    def _watch_timeout(self) -> None:
        assert self._deadline is not None
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                if self._popen.poll() is None:
                    self._timed_out = True
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
                if sig == _sigterm():
                    try:
                        self._popen.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined,unused-ignore]
                        return
                    except Exception:
                        pass
                # Force-kill the whole tree via taskkill.
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
        except Exception:
            pass

    def _check_post_exit(self) -> None:
        if self._timed_out:
            raise TimeoutExpired(
                self.pid,
                self._timeout,  # type: ignore[arg-type,unused-ignore]
                self._build_result(),
            )
        if self._check and self._popen.returncode != 0:
            raise CalledProcessError(
                self._popen.returncode,  # type: ignore[arg-type,unused-ignore]
                self._cmd,
                self._build_result(),
            )

    def _build_result(self) -> Result:
        return Result(
            returncode=(
                self._popen.returncode if self._popen.returncode is not None else -1
            ),
            stdout="\n".join(self._buf_stdout),
            stderr="\n".join(self._buf_stderr),
        )


def _prepare_stdin(stdin: StdinLike) -> tuple:
    """Translate user-provided stdin into (Popen stdin arg, payload-to-write).

    - None  -> DEVNULL, no write
    - str   -> PIPE, write-and-close
    - bytes -> PIPE, write-and-close (decoded later)
    - IO    -> the IO object, no extra write (Popen reads directly)
    """
    if stdin is None:
        return subprocess.DEVNULL, None
    if isinstance(stdin, str):
        return subprocess.PIPE, stdin
    if isinstance(stdin, bytes):
        return subprocess.PIPE, stdin.decode("utf-8", errors="replace")
    # Assume it's a readable IO-like object.
    return stdin, None


def run(
    cmd: CommandLike,
    *,
    cwd: Union[str, Path, None] = None,
    env: Optional[Mapping[str, str]] = None,
    env_add: Optional[Mapping[str, str]] = None,
    shell: bool = False,
    timeout: Optional[float] = None,
    encoding: str = "utf-8",
    errors: str = "replace",
    on_stdout: Optional[LineCallback] = None,
    on_stderr: Optional[LineCallback] = None,
    stdin: StdinLike = None,
    merge_stderr: bool = False,
    check: bool = False,
) -> Process:
    """Spawn `cmd` and return a `Process`.

    The child starts immediately. Output is captured lazily — iterate
    `stream()` to read lines as they arrive, or call `wait()` to block
    until completion and get a `Result`.

    Arguments:
        cmd: A list of args (preferred) or a single string with `shell=True`.
        cwd: Working directory; accepts `str` or `pathlib.Path`.
        env: Full environment mapping. Mutually exclusive with `env_add`.
        env_add: Overlay these keys on top of `os.environ`.
        shell: Pass the command through `/bin/sh -c` (or equivalent).
        timeout: Seconds before the process tree is killed.
        encoding, errors: Passed to Popen; applied to both stdout and stderr.
        on_stdout, on_stderr: Optional per-line callbacks.
        stdin: `None` (DEVNULL), a `str`, a `bytes`, or an IO-like object.
        merge_stderr: Redirect stderr into stdout. All Lines will be tagged
            `"stdout"`.
        check: If True, raise `CalledProcessError` from `wait()` / `stream()`
            when the process exits non-zero.
    """
    return Process(
        cmd,
        cwd=cwd,
        env=env,
        env_add=env_add,
        shell=shell,
        timeout=timeout,
        encoding=encoding,
        errors=errors,
        on_stdout=on_stdout,
        on_stderr=on_stderr,
        stdin=stdin,
        merge_stderr=merge_stderr,
        check=check,
    )


def _sigterm() -> int:
    return signal.SIGTERM


def _sigkill() -> int:
    return getattr(signal, "SIGKILL", signal.SIGTERM)
