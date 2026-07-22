# coding: utf-8
#
# Requires the Android Auto Desktop Head Unit binary to be installed locally
# (Android SDK's extras/google/auto/desktop-head-unit) and a phone connected
# with Android Auto set up. Skipped everywhere else.

import os
import shutil

import pytest

import uiautomator2 as u2

_DHU_AVAILABLE = bool(os.environ.get("DHU_BINARY_PATH")) or shutil.which("desktop-head-unit") is not None


@pytest.fixture
def dhu(d: u2.Device):
    d.dhu.start()
    yield d.dhu
    d.dhu.stop()


@pytest.mark.skipif(not _DHU_AVAILABLE, reason="DHU binary not available in this environment")
def test_dhu_screenshot(dhu):
    im = dhu.screenshot()
    assert im.size[0] > 0
    assert im.size[1] > 0


@pytest.mark.skipif(not _DHU_AVAILABLE, reason="DHU binary not available in this environment")
def test_dhu_tap(dhu):
    # smoke test: sending a tap should not raise
    dhu.tap(10, 10)
