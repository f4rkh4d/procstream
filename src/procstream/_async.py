"""Asynchronous process wrapper built on asyncio.create_subprocess_exec.

Mirrors the sync API where it makes sense: arun() returns an AsyncProcess
with `async for line in p.stream()`, `await p.wait()`, and
`await p.terminate_tree()` / `kill_tree()`.

Unlike the sync variant, there are no reader threads — each pipe is
drained by its own asyncio Task into a single asyncio.Queue that the
user's `async for` pulls from.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Mapping
from pathlib import Path
from typing import (
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
_EOF = object()

AsyncLineCallback = Callable[[str], Union[None, Awaitable[None]]]
CommandLike = Union[str, "list[Union[str, os.PathLike]]"]
StdinLike = Union[str, bytes, None]


def _merge_env(env_add: Optional[Mapping[str, str]]) -> Optional[dict]:
    if env_add is None:
        return None
    merged = os.environ.copy()
    merged.update(env_add)
    return merged


class AsyncProcess:
    """A running or finished subprocess (async)."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        cmd: object,
        timeout: Optional[float],
        check: bool,
        merge_stderr: bool,
    ) -> None:
        self._proc = proc
        self._cmd = cmd
        self._timeout = timeout
        self._check = check
        self._merge_stderr = merge_stderr

        self._q: asyncio.Queue[Union[Line, object]] = asyncio.Queue()
        self._buf_stdout: List[str] = []
        self._buf_stderr: List[str] = []
        self._consumed = False
        self._timed_out = False
        self._threads_total = 1 if merge_stderr else 2

        self._tasks: List[asyncio.Task] = []
        self._tasks.append(
            asyncio.create_task(
                self._drain(proc.stdout, "stdout", self._buf_stdout)
            )
        )
        if not merge_stderr:
            self._tasks.append(
                asyncio.create_task(
                    self._drain(proc.stderr, "stderr", self._buf_stderr)
                )
            )
        if timeout is not None:
            self._tasks.append(asyncio.create_task(self._watch_timeout()))

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    @property
    def running(self) -> bool:
        return self._proc.returncode is None

    async def stream(self) -> AsyncIterator[Line]:
        if self._consumed:
            raise ProcessError("stream() already consumed; call wait() instead")
        self._consumed = True

        exhausted_normally = False
        try:
            done = 0
            while done < self._threads_total:
                item = await self._q.get()
                if item is _EOF:
                    done += 1
                    continue
                yield item  # type: ignore[misc,unused-ignore]
            exhausted_normally = True
        finally:
            await self._proc.wait()

        if exhausted_normally:
            self._check_post_exit()

    async def wait(self) -> Result:
        if not self._consumed:
            self._consumed = True
            done = 0
            while done < self._threads_total:
                item = await self._q.get()
                if item is _EOF:
                    done += 1
        await self._proc.wait()
        self._check_post_exit()
        return self._build_result()

    async def terminate_tree(self, grace: float = 5.0) -> int:
        if self._proc.returncode is not None:
            return self._proc.returncode
        self._signal_group(_sigterm())
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            await self.kill_tree()
        return self._proc.returncode  # type: ignore[return-value,unused-ignore]

    async def kill_tree(self) -> int:
        if self._proc.returncode is None:
            self._signal_group(_sigkill())
        await self._proc.wait()
        return self._proc.returncode  # type: ignore[return-value,unused-ignore]

    async def __aenter__(self) -> AsyncProcess:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._proc.returncode is None:
            await self.terminate_tree(grace=1.0)

    # ---- internal ----

    async def _drain(
        self,
        pipe: Any,
        stream_name: StreamName,
        buf: List[str],
    ) -> None:
        try:
            if pipe is None:
                return
            async for raw in pipe:
                text = (
                    raw.decode("utf-8", errors="replace")
                    if isinstance(raw, (bytes, bytearray))
                    else raw
                )
                line_text = text.rstrip("\r\n") if text.endswith("\n") else text
                buf.append(line_text)
                await self._q.put(Line(line_text, stream_name))
        finally:
            await self._q.put(_EOF)

    async def _watch_timeout(self) -> None:
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=self._timeout)
        except asyncio.TimeoutError:
            if self._proc.returncode is None:
                self._timed_out = True
                self._signal_group(_sigterm())
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._signal_group(_sigkill())

    def _signal_group(self, sig: int) -> None:
        pid = self._proc.pid
        try:
            if _IS_WINDOWS:
                if sig == _sigterm():
                    try:
                        self._proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined,unused-ignore]
                        return
                    except Exception:
                        pass
                import subprocess as _sp

                _sp.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
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
        if self._check and self._proc.returncode != 0:
            raise CalledProcessError(
                self._proc.returncode,  # type: ignore[arg-type,unused-ignore]
                self._cmd,
                self._build_result(),
            )

    def _build_result(self) -> Result:
        return Result(
            returncode=(
                self._proc.returncode if self._proc.returncode is not None else -1
            ),
            stdout="\n".join(self._buf_stdout),
            stderr="\n".join(self._buf_stderr),
        )


async def arun(
    cmd: CommandLike,
    *,
    cwd: Union[str, Path, None] = None,
    env: Optional[Mapping[str, str]] = None,
    env_add: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
    stdin: StdinLike = None,
    merge_stderr: bool = False,
    check: bool = False,
) -> AsyncProcess:
    """Async analog of run().

    Returns an AsyncProcess once the child is spawned. Iterate output
    with `async for line in p.stream()`, or `await p.wait()`.
    """
    if env is not None and env_add is not None:
        raise ValueError("pass either env or env_add, not both")

    effective_env: Optional[Mapping[str, str]]
    if env is not None:
        effective_env = env
    elif env_add is not None:
        effective_env = _merge_env(env_add)
    else:
        effective_env = None

    stdout_target = asyncio.subprocess.PIPE
    stderr_target = (
        asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
    )
    stdin_target = (
        asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
    )

    kwargs: dict = dict(
        stdout=stdout_target,
        stderr=stderr_target,
        stdin=stdin_target,
        cwd=str(cwd) if isinstance(cwd, Path) else cwd,
        env=dict(effective_env) if effective_env is not None else None,
    )
    if _IS_WINDOWS:
        import subprocess as _sp

        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True

    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(
            *[str(a) for a in cmd], **kwargs
        )

    if stdin is not None:
        assert proc.stdin is not None
        payload = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    return AsyncProcess(
        proc,
        cmd=cmd,
        timeout=timeout,
        check=check,
        merge_stderr=merge_stderr,
    )


def _sigterm() -> int:
    return signal.SIGTERM


def _sigkill() -> int:
    return getattr(signal, "SIGKILL", signal.SIGTERM)
