"""Custom SQLAlchemy column types."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from sqlalchemy import SmallInteger, String
from sqlalchemy.types import TypeDecorator


class StrEnumType(TypeDecorator):
    """Persist a ``StrEnum`` as plain ``VARCHAR`` while keeping ``Mapped[MyEnum]`` typing.

    Deliberately not a Postgres ``ENUM``: adding a status later is then a plain
    migration rather than an ``ALTER TYPE`` dance. Validity is enforced by CHECK
    constraints declared alongside the columns, plus the shared state machine.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[StrEnum], length: int = 32) -> None:
        self._enum_cls = enum_cls
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return self._enum_cls(value).value

    def process_result_value(self, value: Any, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        return self._enum_cls(value)


class IntEnumType(TypeDecorator):
    """Persist an ``IntEnum`` as ``SMALLINT`` (used for ``speed_rpm``).

    Keeps the value a number in the database so it can be compared, sorted, and
    arithmetically used by the QC job, while staying a typed enum in Python.
    """

    impl = SmallInteger
    cache_ok = True

    def __init__(self, enum_cls: type[IntEnum]) -> None:
        self._enum_cls = enum_cls
        super().__init__()

    def process_bind_param(self, value: Any, dialect: Any) -> int | None:
        if value is None:
            return None
        return int(self._enum_cls(value))

    def process_result_value(self, value: Any, dialect: Any) -> IntEnum | None:
        if value is None:
            return None
        return self._enum_cls(value)
