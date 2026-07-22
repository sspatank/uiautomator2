# coding: utf-8
#
import os
import subprocess
from unittest.mock import MagicMock, Mock, patch

import cv2
import numpy as np
import pytest
from PIL import Image

from uiautomator2 import dhu as dhu_module
from uiautomator2.dhu import DHU
from uiautomator2.exceptions import DHUBinaryNotFoundError, DHUImageNotFoundError, DHUNotRunningError
from uiautomator2.image import match_multiscale


def _make_device():
    d = Mock()
    d.settings = {"dhu_binary_path": None}
    return d


class TestResolveBinaryPath:
    def test_explicit_binary_path(self, tmp_path):
        binary = tmp_path / "desktop-head-unit"
        binary.write_text("")
        d = _make_device()
        instance = DHU(d, binary_path=str(binary))
        assert instance.resolve_binary_path() == str(binary)

    def test_settings_binary_path(self, tmp_path, monkeypatch):
        binary = tmp_path / "desktop-head-unit"
        binary.write_text("")
        d = Mock()
        d.settings = {"dhu_binary_path": str(binary)}
        monkeypatch.delenv(dhu_module._ENV_BINARY_PATH, raising=False)
        instance = DHU(d)
        assert instance.resolve_binary_path() == str(binary)

    def test_env_var_binary_path(self, tmp_path, monkeypatch):
        binary = tmp_path / "desktop-head-unit"
        binary.write_text("")
        d = _make_device()
        monkeypatch.setenv(dhu_module._ENV_BINARY_PATH, str(binary))
        instance = DHU(d)
        assert instance.resolve_binary_path() == str(binary)

    def test_sdk_root_binary_path(self, tmp_path, monkeypatch):
        sdk_root = tmp_path / "sdk"
        binary_dir = sdk_root / "extras" / "google" / "auto"
        binary_dir.mkdir(parents=True)
        binary = binary_dir / dhu_module._binary_name()
        binary.write_text("")

        d = _make_device()
        monkeypatch.delenv(dhu_module._ENV_BINARY_PATH, raising=False)
        monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk_root))
        instance = DHU(d)
        assert instance.resolve_binary_path() == str(binary)

    def test_not_found_raises(self, monkeypatch):
        d = _make_device()
        monkeypatch.delenv(dhu_module._ENV_BINARY_PATH, raising=False)
        monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
        monkeypatch.delenv("ANDROID_HOME", raising=False)
        monkeypatch.setattr(dhu_module.shutil, "which", lambda name: None)
        instance = DHU(d)
        with pytest.raises(DHUBinaryNotFoundError):
            instance.resolve_binary_path()


class TestLifecycle:
    def test_start_sets_up_forward_and_launches(self, tmp_path, monkeypatch):
        binary = tmp_path / "desktop-head-unit"
        binary.write_text("")
        d = _make_device()
        instance = DHU(d, binary_path=str(binary))
        monkeypatch.setattr(instance, "_kill_existing", lambda: None)

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.poll.return_value = None
        with patch("uiautomator2.dhu.subprocess.Popen", return_value=fake_proc) as mock_popen, \
             patch("uiautomator2.dhu.time.sleep"):
            instance.start(startup_wait=0)

        d.adb_device.forward.assert_called_once_with(
            f"tcp:{dhu_module.DHU_ADB_PORT}", f"tcp:{dhu_module.DHU_ADB_PORT}"
        )
        mock_popen.assert_called_once()
        assert instance.is_running()

    def test_start_noop_if_already_running(self, tmp_path):
        d = _make_device()
        instance = DHU(d, binary_path=str(tmp_path / "desktop-head-unit"))
        instance._proc = MagicMock(spec=subprocess.Popen)
        instance._proc.poll.return_value = None

        with patch("uiautomator2.dhu.subprocess.Popen") as mock_popen:
            instance.start()
        mock_popen.assert_not_called()

    def test_stop_terminates_process(self):
        d = _make_device()
        instance = DHU(d)
        fake_proc = MagicMock(spec=subprocess.Popen)
        instance._proc = fake_proc

        instance.stop()

        fake_proc.terminate.assert_called_once()
        assert instance._proc is None

    def test_is_running_false_initially(self):
        d = _make_device()
        instance = DHU(d)
        assert instance.is_running() is False


class TestCommands:
    def test_tap_requires_running(self):
        d = _make_device()
        instance = DHU(d)
        with pytest.raises(DHUNotRunningError):
            instance.tap(1, 2)

    def test_tap_sends_stdin_command(self):
        d = _make_device()
        instance = DHU(d)
        instance._proc = MagicMock(spec=subprocess.Popen)
        instance._proc.poll.return_value = None
        instance._proc.stdin = MagicMock()

        with patch("uiautomator2.dhu.time.sleep"):
            instance.tap(10, 20)

        instance._proc.stdin.write.assert_called_once_with(b"tap 10 20\n")
        instance._proc.stdin.flush.assert_called_once()

    def test_click_raises_when_not_found(self):
        d = _make_device()
        instance = DHU(d)
        instance._proc = MagicMock(spec=subprocess.Popen)
        instance._proc.poll.return_value = None

        with patch.object(instance, "wait", return_value=None):
            with pytest.raises(DHUImageNotFoundError):
                instance.click("some_template.png", timeout=0.1)

    def test_click_taps_matched_point(self):
        d = _make_device()
        instance = DHU(d)
        with patch.object(instance, "wait", return_value={"similarity": 0.9, "point": [42, 84]}), \
             patch.object(instance, "tap") as mock_tap:
            instance.click("some_template.png")
        mock_tap.assert_called_once_with(42, 84)


class TestMatchMultiscale:
    """
    Template matching (TM_CCOEFF_NORMED) is degenerate for flat, single-color
    patches -- a constant template has zero variance, so normalized
    correlation is undefined and cv2 returns an arbitrary/meaningless
    location. Test fixtures therefore embed a textured (gradient) patch into
    a random-noise background, mirroring real screenshots/icons.
    """

    def _make_target_with_patch(self, size=(300, 300), patch_rect=(120, 120, 40, 40)):
        rng = np.random.default_rng(42)
        w_total, h_total = size
        target = rng.integers(0, 255, size=(h_total, w_total, 3), dtype=np.uint8)
        x, y, w, h = patch_rect
        yy, xx = np.mgrid[0:h, 0:w]
        patch = np.stack([xx * 255 // w, yy * 255 // h, np.full((h, w), 128)], axis=-1).astype(np.uint8)
        target[y:y + h, x:x + w] = patch
        return target, patch

    def test_finds_template_at_native_scale(self):
        target, patch = self._make_target_with_patch()
        result = match_multiscale(patch, target, threshold=0.9, scale_range=(0.9, 1.1), scale_step=0.1)

        assert result is not None
        assert result["similarity"] > 0.9
        x, y = result["point"]
        assert abs(x - 140) <= 3
        assert abs(y - 140) <= 3

    def test_finds_template_at_different_scale(self):
        # A smooth gradient patch is scale-self-similar (a downscaled crop still
        # correlates well against nearby scales/positions), and pure random noise
        # doesn't survive downscale-then-upscale resampling -- so this test uses a
        # coarse, distinctive checkerboard, which is neither.
        rng = np.random.default_rng(42)
        target = rng.integers(0, 255, size=(300, 300, 3), dtype=np.uint8)
        x, y, w, h, cell = 100, 100, 80, 80, 20
        patch = np.zeros((h, w, 3), dtype=np.uint8)
        colors = [(230, 50, 50), (50, 230, 50), (50, 50, 230), (230, 230, 50)]
        for i in range(4):
            for j in range(4):
                patch[i * cell:(i + 1) * cell, j * cell:(j + 1) * cell] = colors[(i + j) % 4]
        target[y:y + h, x:x + w] = patch

        small_template = cv2.resize(patch, (20, 20), interpolation=cv2.INTER_AREA)
        result = match_multiscale(small_template, target, threshold=0.85, scale_range=(0.1, 4.0), scale_step=0.1)

        assert result is not None
        cx, cy = x + w // 2, y + h // 2
        px, py = result["point"]
        assert abs(px - cx) <= 5
        assert abs(py - cy) <= 5

    def test_returns_none_below_threshold(self):
        flat_target = np.zeros((100, 100, 3), dtype=np.uint8)
        rng = np.random.default_rng(1)
        noise_template = rng.integers(0, 255, size=(10, 10, 3), dtype=np.uint8)

        result = match_multiscale(noise_template, flat_target, threshold=0.95)

        assert result is None

    def test_rgba_template_is_alpha_composited(self):
        target, patch = self._make_target_with_patch()
        h, w = patch.shape[:2]
        # Partially transparent (not fully opaque): the composited result should
        # match a template pre-blended 50/50 toward the mid-gray background at
        # the same location -- this only passes if the alpha math actually runs,
        # not merely if the alpha channel is dropped/ignored.
        rgba_template = np.dstack([patch, np.full((h, w), 128, dtype=np.uint8)])
        pre_blended = (patch.astype(np.float32) * 0.5 + 127 * 0.5).astype(np.uint8)

        result = match_multiscale(rgba_template, target, threshold=0.8, scale_range=(0.9, 1.1), scale_step=0.1)
        result_blended = match_multiscale(pre_blended, target, threshold=0.8, scale_range=(0.9, 1.1), scale_step=0.1)

        assert result is not None
        assert result_blended is not None
        assert result["point"] == result_blended["point"]

    def test_rgba_pil_template_is_alpha_composited(self):
        target, patch = self._make_target_with_patch()
        h, w = patch.shape[:2]
        alpha = np.full((h, w), 128, dtype=np.uint8)
        rgb = patch[:, :, ::-1]  # BGR (opencv) -> RGB (PIL)
        pil_template = Image.fromarray(np.dstack([rgb, alpha]), mode="RGBA")

        result = match_multiscale(pil_template, target, threshold=0.8, scale_range=(0.9, 1.1), scale_step=0.1)

        assert result is not None
