# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-04-18

### Added
- **Async API** — `arun()` returns an `AsyncProcess` with `async for line in p.stream()`, `await p.wait()`, `await p.terminate_tree()`, `await p.kill_tree()`, and `async with` context management. Built on `asyncio.create_subprocess_exec` / `_shell`.
- **stdin support** — pass `stdin=` as `str`, `bytes`, or an IO-like object. Default remains `DEVNULL` (immediate EOF in the child).
- **`check=True`** — raise the new `CalledProcessError` from `wait()` / `stream()` when the process exits non-zero. `Result.raise_for_returncode()` exposes the same check on an already-completed result.
- **`merge_stderr=True`** — redirect child stderr into stdout. All yielded `Line`s carry `stream="stdout"`; `Result.stderr` is empty.
- **`env_add=`** — overlay keys on top of `os.environ` without clobbering inheritance. Mutually exclusive with `env=`.
- **`cwd=` accepts `pathlib.Path`** in addition to `str`.
- **`py.typed` marker** — the package is now PEP 561 typed.

### Changed
- Package refactored into `_types`, `_sync`, `_async` modules. Public API unchanged; everything importable from `procstream` top-level as before.
- `run()` gains `stdin`, `merge_stderr`, `check`, `env_add` keyword arguments.

### Fixed
- `stream()` no longer raises `TimeoutExpired` from inside `GeneratorExit` when the caller breaks out early, which previously produced a `PytestUnraisableExceptionWarning` in some interpreters.

## [0.1.0] — 2026-04-18

Initial release.

- Synchronous `run()` / `Process`.
- Streaming `Process.stream()` yields tagged `Line` objects.
- Per-line callbacks via `on_stdout=` / `on_stderr=`.
- Timeouts with escalating SIGTERM → SIGKILL of the whole process group.
- `terminate_tree()` / `kill_tree()` for manual cancel.
- Context manager support.
- Stdlib-only, POSIX + Windows.
