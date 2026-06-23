"""Path resolution: env overrides win; defaults are absolute."""

from __future__ import annotations

from pathlib import Path

import pytest

from kvseo.config import paths


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("KVSEO_DATA_DIR", str(tmp_path / "data"))
    assert paths.config_dir() == tmp_path / "cfg"
    assert paths.config_file() == tmp_path / "cfg" / "config.toml"
    assert paths.data_dir() == tmp_path / "data"
    assert paths.db_path() == tmp_path / "data" / "kvseo.db"


def test_defaults_are_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KVSEO_CONFIG_DIR", raising=False)
    monkeypatch.delenv("KVSEO_DATA_DIR", raising=False)
    assert paths.config_dir().is_absolute()
    assert paths.data_dir().is_absolute()
