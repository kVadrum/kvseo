"""Enable ``python -m kvseo`` as an alias for the ``kvseo`` console script."""

from __future__ import annotations

from kvseo.cli import app

if __name__ == "__main__":
    app()
