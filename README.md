# procstream

Stream subprocess output in Python — with timeouts, tree-kill, and sane defaults.

```python
from procstream import run

for line in run(["pytest", "-q"]).stream():
    prefix = "!!" if line.is_stderr else "  "
    print(prefix, line.text)
```

## Why

`subprocess.run()` buffers everything until the child exits. `subprocess.Popen`
with pipes works, but reading stdout and stderr concurrently without deadlock
is one of those things you have to get right every single time, across every
project. Timeouts that kill *the whole process tree* (not just the direct
child) are another recurring footgun.

`procstream` wraps that pattern into a small, typed, stdlib-only API.

## Install

```bash
pip install procstream
```

Python 3.9+. No runtime dependencies.

## What it gives you

- **Streaming output** — iterate lines as they arrive, tagged by stream (stdout / stderr), in arrival order.
- **Timeouts that actually clean up** — the whole process group is sent SIGTERM, then SIGKILL if it ignores the hint. No orphaned grandchildren.
- **Tree-kill** — `terminate_tree()` / `kill_tree()` for manual cancel, POSIX and Windows.
- **Per-line callbacks** — `on_stdout=print` if you don't want to write the iterator loop yourself.
- **Context manager** — `with run(...) as p:` guarantees the tree dies if your code raises.
- **Result buffering** — `wait()` always returns captured stdout / stderr, even if you streamed them.
- **Stdlib only** — no `psutil`, no surprise deps.

## Usage

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

Lines come out in the same interleaved order the child emitted them. `line.stream`
is `"stdout"` or `"stderr"`.

### Callbacks: no loop required

```python
run(
    ["make", "build"],
    on_stdout=print,
    on_stderr=lambda t: print("!!", t),
).wait()
```

### Timeout: kills the whole tree

```python
from procstream import run, TimeoutExpired

try:
    run(["flaky-script"], timeout=30).wait()
except TimeoutExpired as e:
    print("killed after", e.timeout, "seconds")
    # Partial output is still available:
    print(e.result.stdout)
```

When the deadline fires, procstream sends `SIGTERM` to the process group and
waits up to 2 seconds. If the child still hasn't exited, it sends `SIGKILL`.
Grandchildren die with it.

### Manual cancel

```python
p = run(["long-running-server", "--port", "8080"])
# ... elsewhere
p.terminate_tree(grace=5.0)   # SIGTERM, wait, SIGKILL if needed
# or
p.kill_tree()                  # immediate SIGKILL
```

### Context manager: cancel on exception

```python
with run(["watcher"]) as p:
    for line in p.stream():
        if "ERROR" in line.text:
            raise RuntimeError("saw error, bailing")
# Tree is terminated on the way out.
```

## API

### `run(cmd, *, cwd=None, env=None, shell=False, timeout=None, encoding="utf-8", errors="replace", on_stdout=None, on_stderr=None) -> Process`

Spawn a command and return a `Process`. The child starts immediately.

### `Process`

| member | description |
| --- | --- |
| `pid` | child's pid |
| `returncode` | `None` while running, `int` once finished |
| `running` | `True` while the child is alive |
| `stream()` | iterator of `Line` in arrival order (consume only once) |
| `wait()` | block until completion, return `Result`; raises `TimeoutExpired` if killed |
| `terminate_tree(grace=5.0)` | SIGTERM group, escalate to SIGKILL if still alive |
| `kill_tree()` | SIGKILL group immediately |
| context manager | `with run(...) as p:` — terminates on exit |

### `Line`

| member | description |
| --- | --- |
| `text: str` | line content, newline stripped |
| `stream: "stdout" \| "stderr"` | origin pipe |
| `is_stderr: bool` | shortcut |

### `Result`

| member | description |
| --- | --- |
| `returncode: int` | exit status |
| `stdout: str` | all stdout lines joined with `\n` |
| `stderr: str` | all stderr lines joined with `\n` |
| `combined: str` | stdout followed by stderr (use `stream()` for real order) |
| `ok: bool` | `returncode == 0` |

### `TimeoutExpired`

Raised from `wait()` (or at the end of `stream()`) when `timeout` fires.
Exposes `pid`, `timeout`, and a partial `result`.

## Platform notes

- **POSIX (macOS, Linux):** child starts in a new session via `start_new_session=True`. Signals go to the whole process group with `os.killpg`.
- **Windows:** child starts with `CREATE_NEW_PROCESS_GROUP`. `terminate_tree()` sends `CTRL_BREAK_EVENT`; force-kill falls back to `taskkill /F /T /PID <pid>`.

## License

MIT.
