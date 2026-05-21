"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(monkeypatch, tmp_path):
    """Point every XDG env var at a clean tmp dir for each test."""
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    yield
