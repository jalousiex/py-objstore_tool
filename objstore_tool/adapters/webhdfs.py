"""WebHDFS 适配器。

走 NameNode 的 HTTP REST 接口（``/webhdfs/v1``），只用标准库 http.client，
不引入任何 Hadoop 相关依赖。

两个容易踩的点：
1. ``CREATE`` 是两段式：先向 NameNode 发一个不带 body 的 PUT，拿 307 重定向，
   再往 DataNode 的地址发第二次 PUT 带数据。http.client 不做自动重定向，
   这里手动接管，逻辑反而更清楚。
2. WebHDFS 的布尔参数必须是字面量 ``true`` / ``false``，不能是 Python 的 True。
"""

from __future__ import annotations

import http.client
import json
import ssl
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterator
from urllib.parse import quote, urlencode, urlsplit

from . import register
from .base import Entry, StoreAdapter, StoreError, guess_preview_kind, join_path

WEBHDFS_PREFIX = "/webhdfs/v1"
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300

_EXCEPTION_HINTS = {
    "FileNotFoundException": "路径不存在",
    "AccessControlException": "没有访问权限（检查 user.name 与 HDFS 目录权限）",
    "FileAlreadyExistsException": "目标已存在",
    "SafeModeException": "NameNode 处于安全模式，暂时不接受写操作",
    "ConnectException": "无法连接到 NameNode 或 DataNode",
    "InvalidPathException": "路径格式不合法",
    "PermissionDeniedException": "权限不足",
    "RetriableException": "NameNode 暂时不可用，请稍后重试",
}


def _encode(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode_path(path: str) -> str:
    cleaned = "/" + str(path or "").strip("/")
    return quote(cleaned, safe="/")


def _fmt_mtime(value: Any) -> str | None:
    """HDFS 返回的是毫秒时间戳。"""
    if value in (None, 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


class _Endpoint:
    """把 endpoint 拆成 http.client 需要的连接参数。"""

    def __init__(self, endpoint: str, timeout: int = CONNECT_TIMEOUT):
        parts = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
        self.scheme = (parts.scheme or "http").lower()
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.timeout = timeout
        if not self.host:
            raise StoreError("Endpoint 不合法，请填写形如 http://namenode:9870 的地址")

    def connect(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            context = ssl.create_default_context()
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)


class _WebHDFSClient:
    def __init__(self, conn: dict[str, Any]):
        endpoint = str(conn.get("endpoint") or "")
        self.endpoint = _Endpoint(endpoint)
        self.user = str(conn.get("user") or "hdfs")

    # ---- 基础请求 ----
    def _url(self, path: str, op: str, params: dict[str, Any] | None = None) -> str:
        query: dict[str, str] = {"op": op}
        if self.user:
            query["user.name"] = self.user
        for key, value in (params or {}).items():
            if value is not None:
                query[key] = _encode(value)
        encoded = urlencode(query)
        return f"{self.endpoint.base_path}{WEBHDFS_PREFIX}{_encode_path(path)}?{encoded}"

    def _open(self, method: str, path: str, op: str, params: dict[str, Any] | None = None,
              body: Any = None, headers: dict[str, str] | None = None) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
        conn = self.endpoint.connect()
        try:
            conn.request(method, self._url(path, op, params), body=body, headers=headers or {})
            return conn.getresponse(), conn
        except OSError as exc:
            conn.close()
            raise StoreError(f"无法连接到 {self.endpoint.host}:{self.endpoint.port}", str(exc)) from exc

    @staticmethod
    def _raise_for_remote(status: int, payload: bytes, action: str) -> None:
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
            remote = data.get("RemoteException", {})
            name = str(remote.get("exception") or "").rsplit(".", 1)[-1]
            message = str(remote.get("message") or "")
        except (json.JSONDecodeError, AttributeError):
            name, message = "", payload.decode("utf-8", errors="replace")[:500]

        hint = _EXCEPTION_HINTS.get(name, name or f"HTTP {status}")
        raise StoreError(f"{action}失败：{hint}", message or None)

    def call(self, method: str, path: str, op: str, params: dict[str, Any] | None = None,
             action: str = "操作") -> dict[str, Any]:
        if method == "GET":
            resp, conn = self._open(method, path, op, params)
        else:
            resp, conn = self._open(method, path, op, params, body=b"", headers={"Content-Length": "0"})
        try:
            payload = resp.read()
            if resp.status >= 400:
                self._raise_for_remote(resp.status, payload, action)
            if not payload:
                return {}
            return json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"{action}失败：NameNode 返回了非 JSON 响应", str(exc)) from exc
        finally:
            conn.close()

    # ---- 业务操作 ----
    def test(self) -> dict[str, Any]:
        data = self.call("GET", "/", "GETFILESTATUS", action="连通性测试")
        status = data.get("FileStatus", {})
        return {
            "ok": True,
            "message": f"连接成功，根目录可访问（owner={status.get('owner', '?')}）",
        }

    def list(self, path: str) -> list[Entry]:
        target = path or "/"
        data = self.call("GET", target, "LISTSTATUS", action="列出目录内容")
        statuses = data.get("FileStatuses", {}).get("FileStatus", []) or []
        base = target.rstrip("/")
        entries: list[Entry] = []
        for item in statuses:
            if not isinstance(item, dict):
                continue
            name = str(item.get("pathSuffix") or "")
            if not name:
                continue
            is_dir = str(item.get("type") or "").upper() == "DIRECTORY"
            entries.append(Entry(
                name=name,
                path=f"{base}/{name}" or f"/{name}",
                is_dir=is_dir,
                size=None if is_dir else int(item.get("length") or 0),
                mtime=_fmt_mtime(item.get("modificationTime")),
                etag=None,
                storage_class=str(item.get("permission") or "") or None,
            ))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def stat(self, path: str) -> Entry:
        data = self.call("GET", path, "GETFILESTATUS", action="读取路径信息")
        item = data.get("FileStatus", {}) or {}
        name = str(path).rstrip("/").rsplit("/", 1)[-1] or "/"
        is_dir = str(item.get("type") or "").upper() == "DIRECTORY"
        return Entry(
            name=name,
            path=path,
            is_dir=is_dir,
            size=None if is_dir else int(item.get("length") or 0),
            mtime=_fmt_mtime(item.get("modificationTime")),
            storage_class=str(item.get("permission") or "") or None,
        )

    def mkdir(self, path: str) -> None:
        data = self.call("PUT", path, "MKDIRS", action="创建目录")
        if not data.get("boolean", True):
            raise StoreError("创建目录失败：NameNode 返回 false")

    def delete(self, path: str, is_dir: bool) -> int:
        data = self.call("DELETE", path, "DELETE", params={"recursive": bool(is_dir)}, action="删除")
        if not data.get("boolean", False):
            raise StoreError("删除失败：NameNode 返回 false")
        return 1 if is_dir else 1

    def rename(self, path: str, dest: str) -> None:
        data = self.call("PUT", path, "RENAME", params={"destination": dest}, action="移动")
        if not data.get("boolean", True):
            raise StoreError("移动失败：NameNode 返回 false")

    def open_read(self, path: str, start: int | None = None, length: int | None = None):
        params: dict[str, Any] = {}
        if start:
            params["offset"] = int(start)
        if length:
            params["length"] = int(length)

        resp, conn = self._open("GET", path, "OPEN", params or None)
        if resp.status >= 400:
            payload = resp.read()
            conn.close()
            self._raise_for_remote(resp.status, payload, "读取文件")

        declared = resp.getheader("Content-Length")
        total = int(declared) if declared and declared.isdigit() else None

        def iterator() -> Iterator[bytes]:
            try:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                conn.close()

        return iterator(), total

    def upload(self, path: str, stream: BinaryIO, size: int | None) -> None:
        # 第一段：向 NameNode 申请写入位置
        resp, conn = self._open(
            "PUT", path, "CREATE",
            params={"overwrite": True},
            body=b"",
            headers={"Content-Length": "0"},
        )
        try:
            status = resp.status
            location = resp.getheader("Location")
            if status == 201:
                return  # 极少见：NameNode 直接落盘
            if status >= 400:
                payload = resp.read()
                self._raise_for_remote(status, payload, "上传")
            if status not in (307, 308) or not location:
                raise StoreError(f"上传失败：预期的重定向未出现（HTTP {status}）")
        finally:
            conn.close()

        # 第二段：把数据 PUT 到 DataNode
        target = urlsplit(location)
        scheme = (target.scheme or "http").lower()
        host = target.hostname or self.endpoint.host
        port = target.port or (443 if scheme == "https" else 80)
        conn2 = (
            http.client.HTTPSConnection(host, port, timeout=READ_TIMEOUT, context=ssl.create_default_context())
            if scheme == "https"
            else http.client.HTTPConnection(host, port, timeout=READ_TIMEOUT)
        )
        try:
            headers = {"Content-Type": "application/octet-stream"}
            if size is not None:
                headers["Content-Length"] = str(size)
            conn2.request("PUT", target.path + (f"?{target.query}" if target.query else ""), body=stream, headers=headers)
            resp2 = conn2.getresponse()
            payload = resp2.read()
            if resp2.status >= 400:
                self._raise_for_remote(resp2.status, payload, "上传")
        except OSError as exc:
            raise StoreError(f"无法连接 DataNode {host}:{port}", str(exc)) from exc
        finally:
            conn2.close()

    def preview(self, path: str, max_bytes: int) -> dict[str, Any]:
        kind = guess_preview_kind(path)
        if kind == "text":
            stream, total = self.open_read(path, start=0, length=max_bytes)
            data = b"".join(stream)
            return {
                "kind": "text",
                "text": data.decode("utf-8", errors="replace"),
                "truncated": bool(total and total > len(data)),
                "total": total,
            }
        return {"kind": kind, "raw_url": f"/api/raw?path={path}"}


@register("webhdfs")
class WebHDFSAdapter(StoreAdapter):
    @property
    def supports_presign(self) -> bool:
        return False

    @property
    def root_path(self) -> str:
        return "/"

    def _client(self) -> _WebHDFSClient:
        return _WebHDFSClient(self.conn)

    def test(self) -> dict[str, Any]:
        return self._client().test()

    def list(self, path: str) -> list[Entry]:
        return self._client().list(path or "/")

    def stat(self, path: str) -> Entry:
        return self._client().stat(path)

    def mkdir(self, path: str) -> None:
        self._client().mkdir(path)

    def delete(self, path: str, is_dir: bool) -> int:
        return self._client().delete(path, is_dir)

    def open_read(self, path: str, start: int | None = None, length: int | None = None):
        return self._client().open_read(path, start, length)

    def upload(self, path: str, stream: BinaryIO, size: int | None) -> None:
        self._client().upload(path, stream, size)

    def move(self, src: str, dest_dir: str, is_dir: bool) -> int:
        """HDFS 原生 RENAME：目录与文件都是一次调用，且不搬数据。

        复制没有对应的原生操作，沿用基类的「读出来再写回去」。
        """
        name = str(src).rstrip("/").rsplit("/", 1)[-1]
        self._client().rename(src, join_path(dest_dir, name))
        return 1

    def preview(self, path: str, max_bytes: int) -> dict[str, Any]:
        return self._client().preview(path, max_bytes)
