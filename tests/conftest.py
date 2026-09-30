import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakecam import PASSWORD, USER, FakeCamera  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("REOLINKLINUX_NO_KEYRING", "1")


def _camera(kind):
    cam = FakeCamera(kind)
    yield cam
    cam.close()


@pytest.fixture
def trackmix():
    yield from _camera("trackmix")


@pytest.fixture
def duo2():
    yield from _camera("duo2")


@pytest.fixture
def nvr():
    yield from _camera("nvr")


@pytest.fixture
def creds():
    return USER, PASSWORD
