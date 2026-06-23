"""XDG-style path resolution, cross-platform.

Locations follow the platform conventions (``platformdirs``): on Linux the XDG
base dirs, on macOS ``~/Library/...``, on Windows ``%APPDATA%`` /
``%LOCALAPPDATA%``. Two environment overrides — ``KVSEO_CONFIG_DIR`` and
``KVSEO_DATA_DIR`` — take precedence, which keeps tests hermetic and gives
power users an escape hatch.
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

_APP_NAME = "kvseo"


def config_dir() -> Path:
    """Directory holding ``config.toml`` (and nothing secret)."""
    override = os.environ.get("KVSEO_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_config_dir(_APP_NAME, appauthor=False))


def data_dir() -> Path:
    """Directory holding the SQLite database and other local state."""
    override = os.environ.get("KVSEO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False))


def config_file() -> Path:
    """Path to the TOML config file."""
    return config_dir() / "config.toml"


def db_path() -> Path:
    """Path to the SQLite database file."""
    return data_dir() / "kvseo.db"
