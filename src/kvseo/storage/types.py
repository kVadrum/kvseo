"""Custom SQLAlchemy column types.

``UuidBytes`` stores a UUID as a compact 16-byte ``BLOB`` (data-model §1.2) and
presents it to Python as ``uuid.UUID``. The migration DDL only needs the BLOB
affinity, so migrations use ``sa.LargeBinary`` directly and stay decoupled from
this module.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class UuidBytes(TypeDecorator[uuid.UUID]):
    """UUID <-> 16 raw bytes (BLOB), rendered as canonical UUID in Python."""

    impl = LargeBinary(16)
    cache_ok = True

    def process_bind_param(
        self, value: uuid.UUID | str | None, dialect: Dialect
    ) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value.bytes
        return uuid.UUID(str(value)).bytes

    def process_result_value(
        self, value: Any, dialect: Dialect
    ) -> uuid.UUID | None:
        if value is None:
            return None
        return uuid.UUID(bytes=bytes(value))
