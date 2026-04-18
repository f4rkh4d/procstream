# procstream

[![PyPI version](https://img.shields.io/pypi/v/procstream.svg)](https://pypi.org/project/procstream/)
[![Python versions](https://img.shields.io/pypi/pyversions/procstream.svg)](https://pypi.org/project/procstream/)
[![CI](https://github.com/f4rkh4d/procstream/actions/workflows/ci.yml/badge.svg)](https://github.com/f4rkh4d/procstream/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Type checked](https://img.shields.io/badge/types-mypy-blue.svg)](pyproject.toml)

Stream subprocess output in Python — with timeouts, tree-kill, stdin, `check`, `merge_stderr`, and sane defaults. Sync and async. Stdlib only.

```python
from procstream import run

for line in run(["pytest", "-q"]).stream():
    print("!!" if line.is_stderr else "  ", line.text)
```

## Why

`subprocess.run()` buffers everything until the child exits. `subprocess.Popen`
with pipes works, but reading stdout and stderr concurrently without deadlock
is one of those things you have to get right every single time, across every
project. Timeouts that kill *the whole process tree* (not just the direct
child) are another recurring footgun. Async? Different API again.

`procstream` wraps those patterns into one small, typed, stdlib-only library
with matching sync and async surfaces.

## Install

```bash
pip install procstream
```

Python 3.9+. No runtime dependencies.

## What it gives you

- **Streaming output** — iterate lines as they arrive, tagged by stream (stdout / stderr), in arrival order.
- **Matching sync and async APIs** — `run()` / `arun()` with nearly identical signatures.
- **Timeouts that actually clean up** — the whole process group gets SIGTERM, then SIGKILL if ignored. No orphaned grandchildren.
- **Tree-kill** — `terminate_tree()` / `kill_tree()` for manual cancel, POSIX and Windows.
- **`stdin`** — feed `str`, `bytes`, or an IO object; default is DEVNULL.
- **`check=True`** — raise `CalledProcessError` on non-zero exit.
- **`merge_stderr=True`** — interleave stderr into stdout with one combined stream.
- **`env_add=`** — overlay env vars on top of `os.environ` without replacing it.
- **Per-line callbacks** — `on_stdout=print` if you don't want to write the iterator loop yourself.
- **Context manager** — `with run(...) as p:` (or `async with`) guarantees the tree dies if your code raises.
- **Result buffering** — `wait()` always returns captured stdout / stderr, even if you streamed them.
- **Stdlib only** — no `psutil`, no surprise deps.
- **Fully typed** — PEP 561 `py.typed`.

## Usage — sync

### Simple: capture output

```python
from procstream import run

r = run(["node", "--version"]).wait()
print(r.returncode, r.stdout.strip())
```

### Streaming: react to lines as they arrive

```python
for line in run(["npm", "install"]).stream():
    if line.is_stderr:
        log.warning(line.text)
    else:
        log.info(line.text)
```

### Callbacks

```python
run(
    ["make", "build"],
    on_stdout=print,
    on_stderr=lambda t: print("!!", t),
).wait()
```

### Timeout — kills the whole tree

```python
from procstream import run, TimeoutExpired

try:
    run(["flaky-script"], timeout=30).wait()
except TimeoutExpired as e:
    print("killed after", e.timeout, "seconds")
    print(e.result.stdout)   # partial output still available
```

### `check=True` — raise on non-zero exit

```python
from procstream import run, CalledProcessError

try:
    run(["rspec"], check=True).wait()
except CalledProcessError as e:
    print("tests failed:", e.returncode)
    print(e.result.combined)
```

### stdin

```python
# str
run(["grep", "foo"], stdin="foo\nbar\nfoobar\n").wait()

# bytes
run(["gzip", "-d"], stdin=open("data.gz", "rb").read()).wait()

# a file object
with open("input.txt") as f:
    run(["sort"], stdin=f).wait()
```

### Merge stderr into stdout

```python
for line in run(["cmake", "--build", "."], merge_stderr=True).stream():
    # Every line.stream is "stdout", but the child's stderr is included too.
    print(line.text)
```

### Overlay env vars

```python
r = run(
    ["node", "-e", "console.log(process.env.TOKEN)"],
    env_add={"TOKEN": "shhh"},
).wait()
```

### Manual cancel

```python
p = run(["long-running-server", "--port", "8080"])
# ... elsewhere
p.terminate_tree(grace=5.0)   # SIGTERM, then SIGKILL if still alive
# or
p.kill_tree()                  # immediate SIGKILL
```

### Context manager

```python
with run(["watcher"]) as p:
    for line in p.stream():
        if "ERROR" in line.text:
            raise RuntimeError("bailing")
# Tree is terminated on the way out.
```

## Usage — async

```python
import asyncio
from procstream import arun, TimeoutExpired

async def main():
    # streaming
    proc = await arun(["pytest", "-q"])
    async for line in proc.stream():
        print(line.text)

    # capture
    r = await (await arun(["node", "--version"])).wait()
    print(r.stdout)

    # timeout
    try:
        await (await arun(["sleep", "30"], timeout=1.0)).wait()
    except TimeoutExpired as e:
        print("killed:", e.pid)

    # context manager
    async with await arun(["watcher"]) as p:
        async for line in p.stream():
            if "done" in line.text:
                break

asyncio.run(main())
```

Async flags mirror sync: `stdin`, `check`, `merge_stderr`, `env`, `env_add`, `cwd`, `timeout`.

## API reference

### `run(cmd, *, ...)` → `Process`, `arun(cmd, *, ...)` → `AsyncProcess`

| parameter | type | description |
| --- | --- | --- |
| `cmd` | `list[str]` or `str` | command (with `shell=True` for string form on sync) |
| `cwd` | `str \| Path \| None` | working directory |
| `env` | `Mapping[str, str] \| None` | full environment (replaces inheritance) |
| `env_add` | `Mapping[str, str] \| None` | overlay on `os.environ` |
| `timeout` | `float \| None` | seconds before tree kill |
| `stdin` | `str \| bytes \| IO \| None` | input to feed the child |
| `merge_stderr` | `bool` | redirect stderr into stdout |
| `check` | `bool` | raise `CalledProcessError` on non-zero exit |
| `encoding`, `errors` | `str` | applied to stdout/stderr (sync only) |
| `on_stdout`, `on_stderr` | `Callable[[str], None]` | per-line callbacks (sync only) |

### `Process` / `AsyncProcess`

| member | description |
| --- | --- |
| `pid` | child's pid |
| `returncode` | `None` while running, `int` once finished |
| `running` | `True` while the child is alive |
| `stream()` | (async) iterator of `Line` in arrival order, consume once |
| `wait()` | block/await completion → `Result`; raises on timeout or `check` |
| `terminate_tree(grace=5.0)` | SIGTERM group, escalate to SIGKILL |
| `kill_tree()` | SIGKILL group immediately |
| context manager | `with` (sync) / `async with` (async) — terminates on exit |

### `Line`

`text: str`, `stream: "stdout" \| "stderr"`, `is_stderr: bool`.

### `Result`

`returncode: int`, `stdout: str`, `stderr: str`, `combined: str`, `ok: bool`, `raise_for_returncode(cmd=...)`.

### Exceptions

- `procstream.ProcessError` — base class.
- `procstream.TimeoutExpired(pid, timeout, result)` — raised on timeout.
- `procstream.CalledProcessError(returncode, cmd, result)` — raised when `check=True` and exit is non-zero.

## Platform notes

- **POSIX (macOS, Linux):** child starts in a new session via `start_new_session=True`. Signals go to the whole process group with `os.killpg`.
- **Windows:** child starts with `CREATE_NEW_PROCESS_GROUP`. `terminate_tree()` sends `CTRL_BREAK_EVENT`; force-kill falls back to `taskkill /F /T /PID <pid>`.

## Development

```bash
git clone https://github.com/f4rkh4d/procstream
cd procstream
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
mypy src/procstream
```

## License

MIT.
