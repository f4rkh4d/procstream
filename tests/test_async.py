"""Async API tests."""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent

import pytest

from procstream import CalledProcessError, TimeoutExpired, arun

PY = sys.executable

IS_WINDOWS = sys.platform == "win32"
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only behavior")


def _py(script: str) -> list:
    return [PY, "-c", dedent(script)]


@pytest.mark.asyncio
async def test_arun_captures_stdout():
    p = await arun(_py("print('hello')"))
    r = await p.wait()
    assert r.ok
    assert r.stdout.strip() == "hello"


@pytest.mark.asyncio
async def test_arun_stream_yields_tagged_lines():
    script = """
        import sys
        print('a'); sys.stdout.flush()
        sys.stderr.write('b\\n'); sys.stderr.flush()
        print('c')
    """
    p = await arun(_py(script))
    seen = []
    async for line in p.stream():
        seen.append((line.text, line.stream))

    texts = [t for t, _ in seen]
    streams = dict(seen)
    assert "a" in texts and "b" in texts and "c" in texts
    assert streams["b"] == "stderr"


@pytest.mark.asyncio
async def test_arun_check_raises_on_nonzero():
    p = await arun(_py("import sys; sys.exit(5)"), check=True)
    with pytest.raises(CalledProcessError) as exc:
        await p.wait()
    assert exc.value.returncode == 5


@pytest.mark.asyncio
async def test_arun_timeout_fires():
    p = await arun(_py("import time; time.sleep(30)"), timeout=0.3)
    with pytest.raises(TimeoutExpired) as exc:
        await p.wait()
    assert exc.value.pid == p.pid


@pytest.mark.asyncio
async def test_arun_stdin_str():
    script = """
        import sys
        data = sys.stdin.read()
        print('echo:', data.strip())
    """
    p = await arun(_py(script), stdin="ping")
    r = await p.wait()
    assert "echo: ping" in r.stdout


@pytest.mark.asyncio
async def test_arun_merge_stderr():
    script = """
        import sys
        print('o')
        sys.stderr.write('e\\n')
    """
    p = await arun(_py(script), merge_stderr=True)
    lines = []
    async for line in p.stream():
        lines.append((line.text, line.stream))
    streams = {t: s for t, s in lines}
    assert streams.get("o") == "stdout"
    assert streams.get("e") == "stdout"


@pytest.mark.asyncio
async def test_arun_context_manager_kills_on_exit():
    async with await arun(_py("import time; time.sleep(30)")) as p:
        assert p.running
    assert p.returncode is not None


@pytest.mark.asyncio
async def test_arun_env_add_overlay():
    script = """
        import os
        print('V=' + os.environ.get('MY_ASYNC_VAR', '<missing>'))
    """
    p = await arun(_py(script), env_add={"MY_ASYNC_VAR": "x"})
    r = await p.wait()
    assert "V=x" in r.stdout


@pytest.mark.asyncio
async def test_arun_env_and_env_add_exclusive():
    with pytest.raises(ValueError):
        await arun(_py("pass"), env={}, env_add={"X": "1"})


@pytest.mark.asyncio
@posix_only
async def test_arun_terminate_tree_kills_running():
    p = await arun(_py("import time; time.sleep(30)"))
    await asyncio.sleep(0.1)
    rc = await p.terminate_tree(grace=2.0)
    assert p.returncode is not None
    assert rc == p.returncode
