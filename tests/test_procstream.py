"""Integration-style tests: we actually spawn real subprocesses.

Most tests use short Python one-liners as the target command so they run
on any platform that has pytest + python on PATH. A handful are POSIX-only
and marked accordingly.
"""

import os
import signal
import sys
import time
from textwrap import dedent

import pytest

from procstream import Line, Process, Result, TimeoutExpired, run

IS_WINDOWS = sys.platform == "win32"
PY = sys.executable

posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only behavior")


def _py(script: str) -> list:
    """Build a [python, -c, dedented-script] command list."""
    return [PY, "-c", dedent(script)]


# ---------- basic happy path ----------


def test_run_captures_stdout():
    result = run(_py("print('hello')")).wait()
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.ok


def test_run_captures_stderr():
    result = run(
        _py("import sys; sys.stderr.write('boom\\n'); sys.exit(2)")
    ).wait()
    assert result.returncode == 2
    assert "boom" in result.stderr
    assert not result.ok


def test_returncode_after_wait():
    p = run(_py("import sys; sys.exit(7)"))
    r = p.wait()
    assert r.returncode == 7
    assert p.returncode == 7


# ---------- streaming order ----------


def test_stream_yields_lines_tagged():
    script = """
        import sys, time
        print('one')
        sys.stdout.flush()
        sys.stderr.write('two\\n')
        sys.stderr.flush()
        print('three')
    """
    seen = []
    for line in run(_py(script)).stream():
        assert isinstance(line, Line)
        seen.append((line.text, line.stream))

    texts = [t for t, _ in seen]
    streams_by_text = dict(seen)
    assert "one" in texts
    assert "two" in texts
    assert "three" in texts
    assert streams_by_text["two"] == "stderr"
    assert streams_by_text["one"] == "stdout"


def test_stream_drain_buffers_result():
    lines = list(run(_py("for i in range(3): print(i)")).stream())
    assert [l.text for l in lines] == ["0", "1", "2"]


def test_stream_then_wait_raises():
    p = run(_py("print('x')"))
    list(p.stream())
    # Second consume via stream() should error:
    with pytest.raises(Exception):
        list(p.stream())


# ---------- callbacks ----------


def test_on_stdout_callback_called_per_line():
    received = []
    result = run(
        _py("print('a'); print('b'); print('c')"),
        on_stdout=received.append,
    ).wait()
    assert result.ok
    assert received == ["a", "b", "c"]


def test_callback_exception_does_not_kill_process():
    def bad(_):
        raise RuntimeError("boom")

    # Should not raise; process should still complete.
    result = run(
        _py("print('ok')"),
        on_stdout=bad,
    ).wait()
    assert result.ok
    assert "ok" in result.stdout


# ---------- timeouts ----------


def test_timeout_kills_and_raises():
    p = run(
        _py("import time; time.sleep(30)"),
        timeout=0.3,
    )
    with pytest.raises(TimeoutExpired) as exc:
        p.wait()
    assert exc.value.pid == p.pid
    assert exc.value.timeout == pytest.approx(0.3)


def test_fast_command_does_not_trip_timeout():
    result = run(_py("print('fast')"), timeout=5.0).wait()
    assert result.ok
    assert "fast" in result.stdout


# ---------- manual termination ----------


def test_terminate_tree_exits_cleanly():
    p = run(_py("import time; time.sleep(30)"))
    time.sleep(0.1)
    rc = p.terminate_tree(grace=2.0)
    # POSIX: SIGTERM -> negative returncode. Windows path may differ; both
    # must at least signal "not a normal success".
    assert p.returncode is not None
    assert rc == p.returncode


def test_kill_tree_is_idempotent():
    p = run(_py("import time; time.sleep(30)"))
    p.kill_tree()
    # Second call on a finished process must not raise.
    rc = p.kill_tree()
    assert rc == p.returncode


# ---------- tree-kill semantics (POSIX) ----------


@posix_only
def test_timeout_kills_grandchildren():
    """Spawn a child that spawns a grandchild sleeping for a long time.
    When the timeout fires, the whole group should die — verify by polling
    the grandchild's pid.
    """
    script = """
        import os, time, sys
        pid = os.fork()
        if pid == 0:
            # grandchild
            time.sleep(60)
            sys.exit(0)
        else:
            print(pid, flush=True)
            time.sleep(60)
    """
    p = run(_py(script), timeout=0.8)
    lines = []
    try:
        for line in p.stream():
            lines.append(line.text)
            if lines:
                break
    except TimeoutExpired:
        pass

    # After timeout, the grandchild should be gone too.
    grandchild_pid = int(lines[0])
    time.sleep(0.5)  # give the signal a moment
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


# ---------- context manager ----------


def test_context_manager_kills_on_exit():
    with run(_py("import time; time.sleep(30)")) as p:
        pid = p.pid
        assert p.running
    # Out of the with-block: process must be dead.
    assert p.returncode is not None
    assert not p.running


# ---------- encoding / weirdness ----------


def test_non_utf8_bytes_replaced_not_crashed():
    # 0xff alone is invalid UTF-8. `errors='replace'` must keep us alive.
    script = r"""
        import sys
        sys.stdout.buffer.write(b'\xff\xff\xff')
        sys.stdout.flush()
    """
    result = run(_py(script)).wait()
    assert result.ok
    # Just assert we got *something* and didn't crash.
    assert result.stdout is not None


def test_empty_output():
    result = run(_py("pass")).wait()
    assert result.ok
    assert result.stdout == ""
    assert result.stderr == ""


# ---------- Result helpers ----------


def test_result_combined_and_ok_flags():
    script = """
        import sys
        print('out')
        sys.stderr.write('err\\n')
    """
    r = run(_py(script)).wait()
    assert r.ok
    assert "out" in r.combined
    assert "err" in r.combined
    assert isinstance(r, Result)
