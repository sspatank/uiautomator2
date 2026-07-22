# DHU (Desktop Head Unit) automation

## Context

Google's Android Auto **Desktop Head Unit** (`desktop-head-unit` binary) is driven from uiautomator2 so tests can automate car-UI apps the same way `d.xpath`/`d(...)` automate the phone UI. DHU is fundamentally different from every other integration in this repo: it doesn't expose HTTP/JSONRPC like the on-device uiautomator server, or ADB shell like `u2cli`'s device registry — its only interface is (a) an `--adb=<port>` forwarded connection to the phone and (b) an **interactive stdin console** on the desktop process itself (`tap x y`, `screenshot path`, etc., per https://developer.android.com/training/cars/testing/dhu). Wiring this up means launching the `desktop-head-unit` binary as a subprocess and driving it via its own stdin command protocol — not adding a new uiautomator2 CLI subcommand.

## Design

- **Entry point**: `Device.dhu` as a `cached_property` on `_PluginMixIn` (mirrors `d.screenrecord`, `d.swipe_ext`) — lazy import, one `DHU` instance per `Device`. No global registry needed (unlike `agent_cli`'s `DeviceRegistry`) since DHU is one local desktop process, not a remote server serving multiple clients.
- **Binary discovery**: `DHU.resolve_binary_path()` with fallback order: explicit arg → `d.settings['dhu_binary_path']` → `$DHU_BINARY_PATH` → `$ANDROID_SDK_ROOT`/`$ANDROID_HOME` + `extras/google/auto/desktop-head-unit(.exe)` → `$PATH`. Raises `DHUBinaryNotFoundError` with the list of tried paths if none resolve.
- **Process lifecycle**: `start()`/`stop()`/`is_running()` + `__enter__`/`__exit__`, following the shape (not the internals) of `BasicUiautomatorServer.start_uiautomator/stop_uiautomator` in `uiautomator2/core.py:209-336`. `start()` sets up `adb forward tcp:5277 tcp:5277` via `d.adb_device.forward(...)`, launches `subprocess.Popen(..., stdin=PIPE)`, then polls `self._proc.poll()` after the startup wait and raises `DHUNotRunningError` (with returncode and binary path) if the process died during startup. `stop()` does `terminate()` → `wait()` → fallback `kill()`. `DHU.__init__` registers `atexit.register(self.stop)` so a crashed/killed Python process doesn't leave an orphaned `desktop-head-unit` process on the host.
- **Command protocol**: `_send_cmd(cmd)` writes `f"{cmd}\n"` to `proc.stdin` and flushes; built on top: `tap(x, y)`, `screenshot(format=...)` (via DHU's own `screenshot <path>` command + PIL load), `match()`/`wait()`/`click()` (template matching against DHU screenshots).
- **Image matching**: since DHU screenshots aren't guaranteed to be pixel-identical in scale to a template captured elsewhere, a multi-scale matcher (`uiautomator2/image.py: match_multiscale`) is used instead of the same-resolution `ImageX.match`.
- **Errors**: `uiautomator2/exceptions.py` has a `DHUError` subtree (`DHUBinaryNotFoundError`, `DHUNotRunningError`, `DHUImageNotFoundError`), matching the file's one-liner `class Foo(Bar):...` style and comment-tree header. Only errors actually raised by `dhu.py` are declared.
- **Settings**: `dhu_binary_path` is registered in `uiautomator2/settings.py`'s `_defaults`/`_prop_types`, matching the existing dict-based `Settings` pattern.
- **Optional dependency**: `psutil` (used only for best-effort stale-process cleanup in `_kill_existing()`, which tolerates Linux's truncated 15-char `comm` field via `startswith` rather than equality) is declared `optional = true` under `[tool.poetry.extras] dhu = ["psutil"]`, imported lazily with `try/except ImportError` inside `dhu.py` — not a hard dependency for people who don't use DHU.
- **Tests**: unit tests in `tests/test_dhu.py` are fully mocked (no real binary/device — patch `subprocess.Popen`, `d.settings`, `d.adb_device.forward`); integration tests in `mobile_tests/test_dhu.py` are gated behind `@pytest.mark.skipif` on binary/env availability, following the same pattern as other `mobile_tests/*` files that need real hardware.
- **Docs**: `README.md` has a "Desktop Head Unit (DHU)" section (under Application Management) covering `d.dhu.start()/tap()/screenshot()/click()`, the context-manager form, binary resolution order, and the `dhu_binary_path` setting / `psutil` extra.

Code is Python 3.8-compatible (`Optional[...]`/`Union[...]`, no `X | None`), per `CLAUDE.md`.

## Verification

`poetry run pytest tests/test_dhu.py` passes (17/17, mocked). Real-hardware verification via `mobile_tests/test_dhu.py` is gated behind binary/env availability and hasn't been run.
