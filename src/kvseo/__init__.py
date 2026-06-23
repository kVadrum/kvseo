"""kvseo — an AI-native SEO copilot for solo operators and small agencies.

The canonical version is the ``version`` field in ``pyproject.toml`` (what
/bump and bump-audit edit). ``__version__`` resolves it from installed package
metadata at runtime; the literal below is only a fallback for running against
an uninstalled source tree.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kvseo")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0.0.0"

__all__ = ["__version__"]
