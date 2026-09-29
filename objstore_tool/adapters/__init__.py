"""存储适配层：把不同后端抽象成同一套浏览/读写接口。"""

from __future__ import annotations

from typing import Any

from .base import Entry, StoreAdapter, StoreError

_ADAPTERS: dict[str, type[StoreAdapter]] = {}


def register(kind: str):
    def wrapper(cls: type[StoreAdapter]) -> type[StoreAdapter]:
        cls.kind = kind
        _ADAPTERS[kind] = cls
        return cls

    return wrapper


def build_adapter(conn: dict[str, Any]) -> StoreAdapter:
    """按连接类型构造适配器实例。"""
    from . import s3 as _s3  # noqa: F401  触发注册
    from . import webhdfs as _webhdfs  # noqa: F401

    kind = str(conn.get("type") or "s3")
    cls = _ADAPTERS.get(kind)
    if cls is None:
        raise StoreError(f"不支持的连接类型：{kind}")
    return cls(conn)


__all__ = ["Entry", "StoreAdapter", "StoreError", "register", "build_adapter"]
