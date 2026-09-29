"""本地 HTTP 服务：把 api.py 的逻辑挂到 HTTP 上，并托管前端静态资源。

设计取舍：
- 只用标准库 ``http.server``，不引 Flask/FastAPI —— 单机工具没必要多一层依赖，
  而且打包成 exe 时依赖越少越省心。
- 上传走「原始字节 body + query 参数定位」而不是 multipart：Python 3.13 已移除
  ``cgi`` 模块，自己解析 multipart 得不偿失；前端 fetch 直接传 File 对象即可，
  还能天然支持大文件流式写入。
- 只监听 127.0.0.1，不对外暴露。
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__, api, config as cfg
from .adapters import StoreError

DEFAULT_PORT = 8765
PORT_SCAN_LIMIT = 20


class _BodyReader:
    """按 Content-Length 精确读请求体的包装器。

    ``rfile.read(n)`` 在 keep-alive 连接上会等满 n 字节（socket 不会提前 EOF），
    直接拿它当流用会卡死在最后一个不满的块上；必须按剩余字数封顶。
    ``remaining`` 同时用来判断「body 是否读完」，见 ``_abandon_body``。
    """

    def __init__(self, rfile: BinaryIO, remaining: int):
        self._rfile = rfile
        self.remaining = remaining

    def read(self, amt: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        want = self.remaining if amt is None or amt < 0 else min(amt, self.remaining)
        data = self._rfile.read(want)
        self.remaining -= len(data)
        return data


def _q1(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _qbool(query: dict[str, list[str]], key: str, default: bool = False) -> bool:
    value = _q1(query, key)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


class ObjStoreHandler(BaseHTTPRequestHandler):
    server_version = f"objstore_tool/{__version__}"
    protocol_version = "HTTP/1.1"

    # ---- 日志：只留错误，避免刷屏 ----
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    def log_error(self, fmt: str, *args: Any) -> None:  # noqa: A003
        super().log_error(fmt, *args)

    # ---- HTTP 方法入口 ----
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # ---- 分发 ----
    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        route = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route.startswith("/api/"):
                self._handle_api(method, route[len("/api/"):], query)
            elif method == "GET":
                self._serve_static(route)
            else:
                self._send_json({"error": "不支持的请求方法"}, 405)
        except api.ApiError as exc:
            self._send_json(exc.to_dict(), exc.status)
        except StoreError as exc:
            self._send_json(exc.to_dict(), 502)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程里静默崩溃
            self._send_json({"error": "服务器内部错误", "detail": str(exc)}, 500)

    # ---- API 路由 ----
    def _handle_api(self, method: str, name: str, query: dict[str, list[str]]) -> None:
        if method == "GET":
            if name == "meta":
                return self._send_json(api.get_meta())
            if name == "connections":
                return self._send_json(api.list_connections(reveal=_q1(query, "reveal")))
            if name == "list":
                return self._send_json(api.browse(_q1(query, "conn") or "", _q1(query, "path")))
            if name == "local/list":
                return self._send_json(api.local_list(_q1(query, "path") or ""))
            if name == "local/download":
                return self._serve_local_file(_q1(query, "path") or "")
            if name == "preview":
                return self._send_json(api.preview(_q1(query, "conn") or "", _q1(query, "path") or ""))
            if name == "presign":
                return self._send_json(
                    api.presign(_q1(query, "conn") or "", _q1(query, "path") or "", int(_q1(query, "expires") or 3600))
                )
            if name == "download":
                return self._stream_object(query, inline=False)
            if name == "raw":
                return self._stream_object(query, inline=True)
            if name == "favorites":
                return self._send_json(api.get_favorites())
            if name == "config/export":
                return self._export_config()

        if method == "POST":
            if name == "connections":
                return self._send_json(api.save_connection(self._read_json()))
            if name == "connections/test":
                return self._send_json(api.test_connection(self._read_json()))
            if name == "connections/delete":
                return self._send_json(api.delete_connection(_q1(query, "id") or self._read_json().get("id", "")))
            if name == "mkdir":
                body = self._read_json()
                return self._send_json(api.mkdir(str(body.get("conn") or ""), str(body.get("path") or "")))
            if name == "delete":
                body = self._read_json()
                return self._send_json(api.delete(
                    str(body.get("conn") or ""),
                    str(body.get("path") or ""),
                    bool(body.get("is_dir")),
                ))
            if name in ("copy", "move"):
                body = self._read_json()
                action = api.move if name == "move" else api.copy
                return self._send_json(action(
                    str(body.get("conn") or ""),
                    body.get("sources") or [],
                    str(body.get("dest") or ""),
                ))
            if name in ("local/push", "local/pull"):
                body = self._read_json()
                action = api.pull_local if name.endswith("pull") else api.push_local
                return self._send_json(action(
                    str(body.get("conn") or ""),
                    body.get("sources") or [],
                    str(body.get("dest_dir") or ""),
                ))
            if name == "favorites":
                return self._send_json(api.save_favorites(self._read_json()))
            if name == "config/import":
                payload = self._read_json()
                return self._send_json(api.import_backup(
                    str(payload.get("content") or ""),
                    bool(payload.get("merge", True)),
                ))
            if name == "shutdown":
                return self._shutdown()

        if method == "PUT" and name == "upload":
            return self._receive_upload(query)

        if method == "PUT" and name == "local/upload":
            return self._receive_local_upload(query)

        raise api.ApiError(f"未知接口：/api/{name}", status=404)

    # ---- 上传 ----
    def _receive_upload(self, query: dict[str, list[str]]) -> None:
        size_header = self.headers.get("Content-Length")
        size = int(size_header) if size_header and size_header.isdigit() else None
        path = _q1(query, "path") or ""
        # 有 Content-Length 就套一层，便于出错时判断 body 读没读完（见 _abandon_body）
        body = _BodyReader(self.rfile, size) if size is not None else self.rfile
        try:
            result = api.upload(_q1(query, "conn") or "", path, body, size)
        except Exception:
            self._abandon_body(body)
            raise
        self._send_json(result)

    def _receive_local_upload(self, query: dict[str, list[str]]) -> None:
        size_header = self.headers.get("Content-Length")
        size = int(size_header) if size_header and size_header.isdigit() else 0
        body = _BodyReader(self.rfile, size)
        try:
            result = api.local_upload(_q1(query, "dir") or "", _q1(query, "name") or "", body)
        except Exception:
            self._abandon_body(body)
            raise
        self._send_json(result)

    def _abandon_body(self, body: Any) -> None:
        """出错提前返回时，先把还没读完的请求体读掉丢弃再回错误。

        服务端不读完 body 就应答（例如目标路径非法、连接已不存在），浏览器正在上传
        途中收到应答会判成连接异常 —— 前端只看到「网络错误」，真正的错误信息被丢掉；
        残留在 socket 里的 body 还会被当成下一个请求解析。
        补读丢弃后再应答，浏览器就能正常读到这条错误（本机回环，代价可接受）。
        """
        if getattr(body, "remaining", 0) <= 0:
            return
        try:
            while body.read(1 << 20):
                pass
        except OSError:
            # 客户端已经断开，这条错误发不出去，直接断链收场
            self.close_connection = True

    # ---- 下载 / 原始字节 ----
    def _stream_object(self, query: dict[str, list[str]], inline: bool) -> None:
        conn_id = _q1(query, "conn") or ""
        path = _q1(query, "path") or ""
        stream, size, filename = api.open_download(conn_id, path)
        self._send_stream(stream, size, filename, "inline" if inline else "attachment")

    def _serve_local_file(self, path: str) -> None:
        stream, size, filename = api.local_open_download(path)
        self._send_stream(stream, size, filename, "attachment")

    def _send_stream(self, stream: Any, size: int | None, filename: str, disposition: str) -> None:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{urllib.parse.quote(filename)}")
            self.send_header("Cache-Control", "no-store")
            if size is not None:
                self.send_header("Content-Length", str(size))
            else:
                # 长度未知时退回 HTTP/1.0 语义，靠关闭连接标记结束
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            for chunk in stream:
                self.wfile.write(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()

    # ---- 配置备份 ----
    def _export_config(self) -> None:
        filename, text = api.export_backup()
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- 关闭 ----
    def _shutdown(self) -> None:
        self._send_json({"message": "服务已停止，可以关闭此窗口"})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # ---- 静态资源 ----
    def _serve_static(self, route: str) -> None:
        base = cfg.resource_dir().resolve()
        rel = route.lstrip("/") or "index.html"
        target = (base / rel).resolve()

        # 防目录穿越
        if base not in target.parents and target != base:
            return self._send_json({"error": "非法路径"}, 403)

        if not target.is_file():
            target = base / "index.html"
            if not target.is_file():
                return self._send_json({"error": "前端资源缺失，请确认 web/ 目录存在"}, 500)

        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "application/json"):
            mime += "; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- 工具 ----
    def _read_json(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length")
        if not length or not length.isdigit() or int(length) == 0:
            return {}
        raw = self.rfile.read(int(length))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise api.ApiError("请求体不是合法 JSON", detail=str(exc)) from exc
        if not isinstance(payload, dict):
            raise api.ApiError("请求体应为 JSON 对象")
        return payload

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class ObjStoreServer(ThreadingHTTPServer):
    """本地服务，带独占端口语义。

    踩坑记录（Windows 实测）：``HTTPServer`` 默认 ``allow_reuse_address = True``，
    在 Linux 上这只是复用 TIME_WAIT 端口，但在 Windows 上 ``SO_REUSEADDR`` 的语义是
    「允许端口劫持」——第二个实例对同一端口 bind 会**成功**，却一个连接都收不到。
    结果是 ``create_server`` 里本该触发的「占用则向后探测」在 Windows 上永远不会发生，
    后启动的实例静默失效，用户看到的其实是先启动那个实例的界面与配置。

    修法：Windows 上改用 ``SO_EXCLUSIVEADDRUSE``，端口被占用时 bind 正常报错，
    向后探测得以生效；其余平台保留 ``SO_REUSEADDR``（避免重启被 TIME_WAIT 卡住）。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(host: str = "127.0.0.1", preferred_port: int = DEFAULT_PORT) -> ObjStoreServer:
    """绑定端口，被占用时向后探测若干个。"""
    last_error: OSError | None = None
    for offset in range(PORT_SCAN_LIMIT):
        port = preferred_port + offset
        try:
            return ObjStoreServer((host, port), ObjStoreHandler)
        except OSError as exc:
            last_error = exc
            continue

    raise SystemExit(
        f"端口 {preferred_port}-{preferred_port + PORT_SCAN_LIMIT - 1} 全部被占用，无法启动。\n"
        f"最后一次错误：{last_error}"
    )
