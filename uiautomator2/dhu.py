# coding: utf-8
#
# Automates the Android Auto Desktop Head Unit (DHU) emulator.
#
# DHU renders car-UI (media/nav templates) in its own window on the host
# desktop -- completely separate from the phone's screen -- so screenshots
# and taps here go through DHU's own interactive stdin protocol
# (`screenshot <path>`, `tap <x> <y>`, sent as line commands once the binary
# is launched with stdin=PIPE), not adb or the on-device uiautomator server.
# Ref: https://developer.android.com/training/cars/testing/dhu

import atexit
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, List, Optional, Union

import cv2
import numpy as np
from PIL import Image

from uiautomator2.exceptions import (
    DHUBinaryNotFoundError,
    DHUImageNotFoundError,
    DHUNotRunningError,
)

if TYPE_CHECKING:
    import uiautomator2

logger = logging.getLogger(__name__)

DHU_ADB_PORT = 5277

_ENV_BINARY_PATH = "DHU_BINARY_PATH"
_ENV_SDK_ROOTS = ("ANDROID_SDK_ROOT", "ANDROID_HOME")
_RELATIVE_BINARY_DIR = os.path.join("extras", "google", "auto")


def _binary_name() -> str:
    return "desktop-head-unit.exe" if os.name == "nt" else "desktop-head-unit"


class DHU:
    """
    Args:
        d: owning uiautomator2.Device, used for adb_device (port forwarding) access
        binary_path: explicit path to the `desktop-head-unit` binary. If not given,
            resolved lazily by `resolve_binary_path()`.
        adb_port: local/remote tcp port used for `adb forward` (DHU's default is 5277)
    """

    def __init__(self, d: "uiautomator2.Device", binary_path: Optional[str] = None, adb_port: int = DHU_ADB_PORT):
        self._d = d
        self._binary_path = binary_path
        self._adb_port = adb_port
        self._proc: Optional[subprocess.Popen] = None
        atexit.register(self.stop)

    def resolve_binary_path(self) -> str:
        """
        Resolution order:
            1. binary_path passed to __init__
            2. d.settings['dhu_binary_path']
            3. $DHU_BINARY_PATH env var
            4. $ANDROID_SDK_ROOT or $ANDROID_HOME + extras/google/auto/desktop-head-unit(.exe)
            5. desktop-head-unit(.exe) on $PATH

        Raises:
            DHUBinaryNotFoundError
        """
        name = _binary_name()
        tried = []

        candidates = [
            self._binary_path,
            self._d.settings.get("dhu_binary_path"),
            os.environ.get(_ENV_BINARY_PATH),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            tried.append(candidate)
            if os.path.isfile(candidate):
                return candidate

        for sdk_env in _ENV_SDK_ROOTS:
            sdk_root = os.environ.get(sdk_env)
            if not sdk_root:
                continue
            candidate = os.path.join(sdk_root, _RELATIVE_BINARY_DIR, name)
            tried.append(candidate)
            if os.path.isfile(candidate):
                return candidate

        which_path = shutil.which(name)
        if which_path:
            return which_path
        tried.append(f"$PATH ({name})")

        raise DHUBinaryNotFoundError(
            f"Could not locate the DHU binary ({name}). Tried: {tried}. "
            f"Pass binary_path=, set d.settings['dhu_binary_path'], or set ${_ENV_BINARY_PATH}."
        )

    def _kill_existing(self) -> None:
        """ Best-effort cleanup of any already-running DHU process (cross-platform). """
        try:
            import psutil
        except ImportError:
            logger.debug("psutil not installed, skipping cleanup of existing DHU process")
            return

        name = _binary_name().lower()
        # Linux truncates /proc/<pid>/comm (and thus psutil's reported name) to
        # 15 chars, so also accept an exact match against that truncated form --
        # a plain `name.startswith(proc_name)` would match any short unrelated
        # process name (e.g. "d", "desktop") and kill it.
        truncated_name = name[:15]
        for proc in psutil.process_iter(["name"]):
            try:
                proc_name = proc.info["name"]
                if proc_name and proc_name.lower() in (name, truncated_name):
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def start(self, extra_args: Optional[List[str]] = None, startup_wait: float = 5.0) -> None:
        """
        Kill any existing DHU process, set up `adb forward tcp:{port} tcp:{port}`,
        then launch the DHU binary with a controlled stdin pipe.

        No-op if already running (call `stop()` first to relaunch).
        """
        if self.is_running():
            return

        binary_path = self.resolve_binary_path()
        self._kill_existing()

        self._d.adb_device.forward(f"tcp:{self._adb_port}", f"tcp:{self._adb_port}")

        args = [binary_path, f"--adb={self._adb_port}", *(extra_args or [])]
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(binary_path) or None,
        )
        time.sleep(startup_wait)

        returncode = self._proc.poll()
        if returncode is not None:
            self._proc = None
            raise DHUNotRunningError(
                f"DHU process exited during startup (returncode={returncode}), binary_path={binary_path!r}"
            )

    def stop(self, timeout: float = 5.0) -> None:
        """ Terminate the DHU process started by `start()`, if any. """
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=timeout)
        self._proc = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> "DHU":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _send_cmd(self, cmd: str, wait: float = 1.5) -> None:
        if not self.is_running():
            raise DHUNotRunningError("DHU is not running, call d.dhu.start() first")
        assert self._proc.stdin is not None
        self._proc.stdin.write(f"{cmd}\n".encode())
        self._proc.stdin.flush()
        time.sleep(wait)

    def tap(self, x: int, y: int) -> None:
        """ Tap the DHU window at window-local coordinates (x, y). """
        self._send_cmd(f"tap {x} {y}")

    def screenshot(self, format: str = "pillow") -> Union[Image.Image, np.ndarray]:
        """
        Capture the current DHU screen via DHU's own `screenshot` stdin command.

        Args:
            format: "pillow" (default) or "opencv"
        """
        fd, path = tempfile.mkstemp(suffix=".png", prefix="u2_dhu_")
        os.close(fd)
        try:
            self._send_cmd(f"screenshot {path}", wait=1.0)
            im = Image.open(path)
            im.load()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        if format == "opencv":
            return cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
        return im

    def match(self, imdata, threshold: float = 0.8) -> Optional[dict]:
        """
        Multi-scale template match `imdata` against the current DHU screenshot.

        Returns:
            {"similarity": float, "point": [x, y]} or None if not found
        """
        from uiautomator2.image import match_multiscale
        target = self.screenshot(format="opencv")
        return match_multiscale(imdata, target, threshold=threshold)

    def wait(self, imdata, timeout: float = 30.0, threshold: float = 0.8, interval: float = 1.0) -> Optional[dict]:
        """ Poll `match()` until it succeeds or `timeout` elapses. """
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self.match(imdata, threshold=threshold)
            if m is not None:
                return m
            time.sleep(interval)
        return None

    def click(self, imdata, timeout: float = 30.0, threshold: float = 0.8) -> None:
        """
        Wait for `imdata` to appear on the DHU screen, then tap its center.

        Raises:
            DHUImageNotFoundError: if not found within `timeout` seconds
        """
        m = self.wait(imdata, timeout=timeout, threshold=threshold)
        if m is None:
            raise DHUImageNotFoundError(f"image not found on DHU screen within {timeout:.0f}s")
        x, y = m["point"]
        self.tap(x, y)
