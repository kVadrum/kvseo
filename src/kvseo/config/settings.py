"""Typed configuration schema, loaded from ``config.toml``.

The schema is intentionally small for v0.1 (advisor / storage / report).
Connector sections and the ``env:`` secret-indirection resolver land in the
connector build; secrets are never stored in this file (they live in the OS
keyring — see 02-architecture.md §7).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class AdvisorSettings(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5"
    max_tokens: int = 4096
    temperature: float = 0.2
    cache_enabled: bool = False


class StorageSettings(BaseModel):
    path: str | None = None  # None → resolve from kvseo.config.paths.db_path()
    litestream_enabled: bool = False
    litestream_url: str = ""


class ReportSettings(BaseModel):
    default_format: str = "html"  # v0.1: md | html


class Settings(BaseModel):
    advisor: AdvisorSettings = Field(default_factory=AdvisorSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)

    @classmethod
    def load(cls, path: Path) -> Settings:
        """Load settings from a TOML file, falling back to defaults if absent."""
        if not path.exists():
            return cls()
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return cls.model_validate(data)


# Written verbatim by `kvseo init`. tomllib is read-only (stdlib has no writer),
# so the default file is a hand-maintained template rather than serialized.
DEFAULT_CONFIG_TOML = """\
# kvseo configuration — https://github.com/kvadrum/kvseo
# Secrets are NOT stored here; they live in your OS keyring.

[advisor]
provider = "anthropic"        # passed to LiteLLM as the model prefix
model = "claude-haiku-4-5"    # cheapest decent tier; swap to any LiteLLM model
max_tokens = 4096
temperature = 0.2
cache_enabled = false

[storage]
litestream_enabled = false    # opt-in S3 replication; see the README
litestream_url = ""

[report]
default_format = "html"       # v0.1: md | html

# Connectors are configured via `kvseo connect <name>` (v0.1 build).
# [connectors.gsc]
# property = "https://example.com/"
# [connectors.psi]
# api_key = "env:PSI_API_KEY"
"""
