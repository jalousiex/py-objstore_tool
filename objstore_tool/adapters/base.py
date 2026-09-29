"""统一适配器接口。

路径约定（全项目统一）：
- 使用 POSIX 风格斜杠路径，不带首尾多余的斜杠（根路径除外）。
- S3 兼容存储：路径第一段是桶名，例如 ``mybucket/logs/2026/``。
  空字符串 ``""`` 表示「桶列表」这一层。
- WebHDFS：路径是 NameNode 上的绝对路径，例如 ``/user/hdfs/warehouse``，
  根路径为 ``"/"``。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass
from typing import Any, BinaryIO, Callable, Iterator


class StoreError(Exception):
    """适配层统一异常，message 是可直接展示给用户的中文说明。"""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        payload = {"error": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class ProgressReader:
    """只读流包装：按已读字节数增量上报进度（适配器向远端传数据时用它计数）。"""

    def __init__(self, stream: BinaryIO, on_bytes: Callable[[int], None]):
        self._stream = stream
        self._on_bytes = on_bytes

    def read(self, amt: int = -1) -> bytes:
        data = self._stream.read(amt)
        if data:
            self._on_bytes(len(data))
        return data

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


@dataclass
class Entry:
    """统一的目录/文件条目。"""

    name: str
    path: str
    is_dir: bool
    size: int | None = None
    mtime: str | None = None
    etag: str | None = None
    storage_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 预览类型判定：放在基类里，S3 与 WebHDFS 共用同一套规则
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif"}
TEXT_EXTS = {
    ".txt",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".xml",
    ".yaml",
    ".yml",
    ".sql",
    ".md",
    ".ini",
    ".conf",
    ".cfg",
    ".properties",
    ".env",
    ".sh",
    ".bat",
    ".ps1",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".scala",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".r",
    ".toml",
    ".html",
    ".htm",
    ".css",
    ".gitignore",
}


def guess_preview_kind(path: str) -> str:
    """按扩展名猜测预览方式，返回 text / image / binary。"""
    ext = os.path.splitext(str(path).rsplit("/", 1)[-1])[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    return "binary"


def join_path(*parts: str) -> str:
    """拼接 POSIX 路径，忽略空段，保留前导斜杠语义。"""
    cleaned = [str(p).strip("/") for p in parts if p is not None and str(p).strip("/")]
    return "/".join(cleaned)


class StoreAdapter:
    """存储适配器基类。"""

    kind = "base"

    def __init__(self, conn: dict[str, Any]):
        self.conn = conn

    # ---- 元信息 ----
    @property
    def supports_presign(self) -> bool:
        return False

    @property
    def root_path(self) -> str:
        return ""

    @property
    def label(self) -> str:
        return f"{self.conn.get('name')}（{self.conn.get('endpoint')}）"

    # ---- 需要子类实现 ----
    def test(self) -> dict[str, Any]:
        """连通性测试，成功返回附加信息（如桶数量）。"""
        raise NotImplementedError

    def list(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def stat(self, path: str) -> Entry:
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def delete(self, path: str, is_dir: bool) -> int:
        """删除，返回删除的对象数量。"""
        raise NotImplementedError

    def open_read(
        self, path: str, start: int | None = None, length: int | None = None
    ) -> tuple[Iterator[bytes], int | None]:
        """返回 (字节流迭代器, 总长度)。start/length 用于 Range 预览。"""
        raise NotImplementedError

    def upload(
        self,
        path: str,
        stream: BinaryIO,
        size: int | None,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        raise NotImplementedError

    def presign(self, path: str, expires: int = 3600) -> str:
        raise StoreError("当前存储类型不支持生成预签名链接")

    def preview(self, path: str, max_bytes: int) -> dict[str, Any]:
        """轻量预览：文本截断返回，图片等直接给下载地址。默认走 open_read。"""
        stream, total = self.open_read(path, start=0, length=max_bytes)
        data = b"".join(stream)
        truncated = bool(total and total > len(data))
        return {
            "kind": "text",
            "text": data.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "total": total,
        }

    # ---- 复制 / 移动 ----
    def copy(self, src: str, dest_dir: str, is_dir: bool) -> int:
        """把 src 复制成 dest_dir 下的同名子项，返回复制的对象数。

        默认实现是「读出来再写回去」的通用流复制，数据经本地中转；
        支持服务端复制的适配器（如 S3）会覆盖它，不占本地带宽。
        """
        name = str(src).rstrip("/").rsplit("/", 1)[-1]
        dest = join_path(dest_dir, name)
        if not is_dir:
            self._copy_one(src, dest)
            return 1

        self.mkdir(dest)  # 空目录也要在目标侧留下同名目录
        made: set[str] = {dest}
        count = 0
        for rel in self._walk_files(src):
            target = join_path(dest, rel)
            parent = target.rsplit("/", 1)[0]
            if parent not in made:
                self.mkdir(parent)  # 子目录要逐层建好（HDFS 不会替你建父目录）
                made.add(parent)
            self._copy_one(join_path(src, rel), target)
            count += 1
        return count

    def move(self, src: str, dest_dir: str, is_dir: bool) -> int:
        """默认实现：复制过去再删源。S3 的复制走服务端 copy，这里同样适用。"""
        count = self.copy(src, dest_dir, is_dir)
        self.delete(src, is_dir)
        return count

    def _walk_files(self, path: str) -> list[str]:
        """递归列出目录下所有文件的相对路径（目录本身不返回）。"""
        out: list[str] = []

        def walk(current: str, prefix: str) -> None:
            for entry in self.list(current):
                rel = f"{prefix}{entry.name}"
                if entry.is_dir:
                    walk(entry.path, f"{rel}/")
                else:
                    out.append(rel)

        walk(path, "")
        return out

    def _copy_one(self, src_path: str, dest_path: str) -> None:
        """复制单个对象：先落临时文件再上传，避免把大文件整个读进内存。"""
        stream, size = self.open_read(src_path)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".copy") as tmp:
                tmp_path = tmp.name
                for chunk in stream:
                    tmp.write(chunk)
            with open(tmp_path, "rb") as fh:
                self.upload(dest_path, fh, size)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---- 通用工具 ----
    def describe(self) -> dict[str, Any]:
        return {
            "id": self.conn.get("id"),
            "name": self.conn.get("name"),
            "type": self.conn.get("type"),
            "endpoint": self.conn.get("endpoint"),
            "root_path": self.root_path,
            "supports_presign": self.supports_presign,
        }
