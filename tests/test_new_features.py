"""Tests for v0.2 features: stdin, check, merge_stderr, env_add, pathlib cwd."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from procstream import CalledProcessError, run

PY = sys.executable


def _py(script: str) -> list:
    return [PY, "-c", dedent(script)]


# ---------- stdin ----------


def test_stdin_str_written_to_child():
    result = run(
        _py(
            """
            import sys
            data = sys.stdin.read()
            print('got:', data.strip())
            """
        ),
        stdin="hello from stdin",
    ).wait()
    assert result.ok
    assert "got: hello from stdin" in result.stdout


def test_stdin_bytes_written_to_child():
    result = run(
        _py(
            """
            import sys
            data = sys.stdin.read()
            print('len:', len(data))
            """
        ),
        stdin=b"abc\ndef\n",
    ).wait()
    assert result.ok
    assert "len: 8" in result.stdout


def test_no_stdin_default_is_devnull():
    # If stdin were a TTY/pipe, reading would hang or return empty. DEVNULL
    # gives immediate EOF.
    result = run(
        _py(
            """
            import sys
            data = sys.stdin.read()
            print('len:', len(data))
            """
        )
    ).wait()
    assert result.ok
    assert "len: 0" in result.stdout


# ---------- check=True ----------


def test_check_true_raises_on_nonzero():
    with pytest.raises(CalledProcessError) as exc:
        run(_py("import sys; sys.exit(3)"), check=True).wait()
    assert exc.value.returncode == 3
    assert exc.value.result.returncode == 3


def test_check_true_does_not_raise_on_success():
    r = run(_py("print('ok')"), check=True).wait()
    assert r.ok
    assert "ok" in r.stdout


def test_check_true_via_stream_iteration():
    p = run(_py("import sys; print('x'); sys.exit(4)"), check=True)
    collected = []
    with pytest.raises(CalledProcessError):
        for line in p.stream():
            collected.append(line.text)
    assert "x" in collected
    assert p.returncode == 4


def test_result_raise_for_returncode():
    r = run(_py("import sys; sys.exit(2)")).wait()
    assert not r.ok
    with pytest.raises(CalledProcessError):
        r.raise_for_returncode(cmd="[demo]")


# ---------- merge_stderr ----------


def test_merge_stderr_combines_streams():
    script = """
        import sys
        print('out-line')
        sys.stderr.write('err-line\\n')
        sys.stderr.flush()
    """
    lines = list(run(_py(script), merge_stderr=True).stream())
    # When merged, every Line is tagged as stdout.
    assert all(l.stream == "stdout" for l in lines)
    texts = [l.text for l in lines]
    assert "out-line" in texts
    assert "err-line" in texts


def test_merge_stderr_result_populated():
    script = """
        import sys
        print('out')
        sys.stderr.write('err\\n')
    """
    r = run(_py(script), merge_stderr=True).wait()
    assert r.ok
    # Both ended up on stdout since stderr was redirected there.
    assert "out" in r.stdout
    assert "err" in r.stdout
    # Stderr buffer is empty in merged mode.
    assert r.stderr == ""


# ---------- env_add ----------


def test_env_add_overlays_os_environ():
    # PATH should survive (from os.environ inheritance) *and* the new var
    # should be set.
    script = """
        import os
        print('PATH=' + ('yes' if os.environ.get('PATH') else 'no'))
        print('CUSTOM=' + os.environ.get('CUSTOM_VAR', '<missing>'))
    """
    r = run(_py(script), env_add={"CUSTOM_VAR": "hello"}).wait()
    assert "PATH=yes" in r.stdout
    assert "CUSTOM=hello" in r.stdout


def test_env_replaces_completely():
    # When env= is supplied (not env_add), inheritance is dropped.
    script = """
        import os
        print('CUSTOM=' + os.environ.get('CUSTOM_VAR', '<missing>'))
    """
    # Pass an env that doesn't carry CUSTOM_VAR but needs PATH on Windows for
    # python to work — supply both explicitly.
    extra = {"PATH": os.environ.get("PATH", ""), "CUSTOM_VAR": "replaced-env"}
    r = run(_py(script), env=extra).wait()
    assert "CUSTOM=replaced-env" in r.stdout


def test_env_and_env_add_mutually_exclusive():
    with pytest.raises(ValueError):
        run(_py("pass"), env={}, env_add={"X": "1"}).wait()


# ---------- pathlib cwd ----------


def test_cwd_accepts_pathlib_path():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "marker.txt").write_text("hello")
        script = """
            import os
            print(sorted(os.listdir('.')))
        """
        r = run(_py(script), cwd=tmpdir).wait()
        assert "marker.txt" in r.stdout


def test_cwd_accepts_string():
    with tempfile.TemporaryDirectory() as tmp:
        r = run(
            _py("import os; print('cwd-basename=' + os.path.basename(os.getcwd()))"),
            cwd=tmp,
        ).wait()
        assert "cwd-basename=" in r.stdout
