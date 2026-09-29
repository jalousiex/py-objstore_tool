"""WebHDFS 端到端自测：起一个 mock NameNode，跑通浏览 / 上传 / 下载 / 删除。

    uv run python tests/selftest_webhdfs.py

重点验证两件事：
1. LISTSTATUS / GETFILESTATUS 的 JSON 解析与路径拼接
2. CREATE 的两段式写入（NameNode 307 重定向 → DataNode 落数据）

mock 把 DataNode 简化成同一进程里的 /datanode 前缀，只为验证重定向逻辑本身。
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from objstore_tool import api, config as cfg  # noqa: E402
from objstore_tool.adapters import build_adapter  # noqa: E402

NOW_MS = 1758000000000


def _file(content: bytes) -> dict:
    return {"type": "FILE", "length": len(content), "content": content, "modificationTime": NOW_MS}


def _dir() -> dict:
    return {"type": "DIRECTORY", "length": 0, "content": b"", "modificationTime": NOW_MS}


FS: dict[str, dict] = {
    "/": _dir(),
    "/tmp": _dir(),
    "/user": _dir(),
    "/user/hdfs": _dir(),
    "/user/hdfs/readme.txt": _file("HDFS mock 根文件\n".encode("utf-8")),
    "/user/hdfs/warehouse": _dir(),
    "/user/hdfs/warehouse/ods": _dir(),
    "/user/hdfs/warehouse/ods/t1.csv": _file(b"id,name\n1,alpha\n"),
    "/user/hdfs/warehouse/ods/t2.csv": _file(b"id,name\n2,beta\n"),
}


class MockNameNode(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102
        pass

    # ---- 解析 ----
    def _parse(self) -> tuple[bool, str, dict]:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        path = urllib.parse.unquote(parsed.path)
        if path.startswith("/datanode"):
            return True, path[len("/datanode"):], query
        if path.startswith("/webhdfs/v1"):
            return False, path[len("/webhdfs/v1"):] or "/", query
        return False, path, query

    @staticmethod
    def _norm(path: str) -> str:
        return path.rstrip("/") or "/"

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        _, path, query = self._parse()
        op = (query.get("op") or [""])[0]
        if op == "GETFILESTATUS":
            return self._filestatus(path)
        if op == "LISTSTATUS":
            return self._liststatus(path)
        if op == "OPEN":
            return self._open(path, query)
        return self._remote_error(400, "UnsupportedOperationException", f"op={op}")

    # ---- PUT ----
    def do_PUT(self):  # noqa: N802
        is_datanode, path, query = self._parse()
        op = (query.get("op") or [""])[0]

        # 第二段：DataNode 落数据
        if is_datanode and op == "CREATE":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            target = self._norm(path)
            FS[target] = _dir() if path.endswith("/") and not body else _file(body)
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # 第一段：NameNode 给 307
        if op == "CREATE":
            port = self.server.server_address[1]
            # Location 会被塞进 HTTP 响应头，头只能是 latin-1；中文路径必须百分号编码
            encoded = urllib.parse.quote(path, safe="/")
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{port}/datanode{encoded}?op=CREATE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if op == "MKDIRS":
            current = ""
            for part in [p for p in path.strip("/").split("/") if p]:
                current += "/" + part
                FS.setdefault(current, _dir())
            return self._json(200, {"boolean": True})

        return self._remote_error(400, "UnsupportedOperationException", f"op={op}")

    # ---- DELETE ----
    def do_DELETE(self):  # noqa: N802
        _, path, query = self._parse()
        recursive = (query.get("recursive") or ["false"])[0].lower() == "true"
        target = self._norm(path)
        info = FS.get(target)
        if info is None:
            return self._remote_error(404, "FileNotFoundException", target)

        if info["type"] == "DIRECTORY":
            children = [k for k in FS if k == target or k.startswith(target.rstrip("/") + "/")]
            if not recursive and len(children) > 1:
                return self._remote_error(403, "AccessControlException", "目录非空且未开启递归")
            for key in children:
                FS.pop(key, None)
        else:
            FS.pop(target, None)
        return self._json(200, {"boolean": True})

    # ---- 动作 ----
    def _filestatus(self, path: str):
        target = self._norm(path)
        info = FS.get(target)
        if info is None:
            return self._remote_error(404, "FileNotFoundException", target)
        return self._json(200, {"FileStatus": {
            "type": info["type"],
            "length": info["length"],
            "modificationTime": info["modificationTime"],
            "permission": "755" if info["type"] == "DIRECTORY" else "644",
            "owner": "hdfs",
            "group": "supergroup",
        }})

    def _liststatus(self, path: str):
        target = self._norm(path)
        if target not in FS:
            return self._remote_error(404, "FileNotFoundException", target)

        prefix = "/" if target == "/" else target + "/"
        base_len = len(prefix)
        items = []
        for key, info in sorted(FS.items()):
            if key == target or not key.startswith(prefix):
                continue
            suffix = key[base_len:]
            if not suffix or "/" in suffix:
                continue
            items.append({
                "pathSuffix": suffix,
                "type": info["type"],
                "length": info["length"],
                "modificationTime": info["modificationTime"],
                "permission": "755" if info["type"] == "DIRECTORY" else "644",
                "owner": "hdfs",
                "group": "supergroup",
                "replication": 0,
                "blockSize": 0,
            })
        return self._json(200, {"FileStatuses": {"FileStatus": items}})

    def _open(self, path: str, query: dict):
        target = self._norm(path)
        info = FS.get(target)
        if info is None or info["type"] != "FILE":
            return self._remote_error(404, "FileNotFoundException", target)

        data = info["content"]
        offset = int((query.get("offset") or ["0"])[0] or 0)
        length_raw = query.get("length")
        if length_raw:
            data = data[offset: offset + int(length_raw[0])]
        elif offset:
            data = data[offset:]

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 响应 ----
    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _remote_error(self, status: int, exception: str, message: str):
        cls = f"org.apache.hadoop.hdfs.protocol.{exception}"
        self._json(status, {"RemoteException": {
            "exception": cls,
            "javaClassName": cls,
            "message": message,
        }})


def start_mock() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockNameNode)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


PASS, FAIL = [], []


def check(label: str, condition: bool, extra: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   {extra}" if extra else ""))


def main() -> int:
    server, endpoint = start_mock()
    print(f"mock NameNode 已启动：{endpoint}\n")

    conn = {
        "id": "selftest-hdfs",
        "type": "webhdfs",
        "name": "自测 mock HDFS",
        "endpoint": endpoint,
        "user": "hdfs",
    }
    adapter = build_adapter(conn)

    # 「本地目录 → 远端」用例的源目录树（放到临时目录，跑完删）
    local_root = Path(tempfile.mkdtemp(prefix="objstore-push-hdfs-"))
    local_src = local_root / "pushed"
    (local_src / "sub").mkdir(parents=True)
    (local_src / "empty").mkdir()
    (local_src / "a.txt").write_text("A", encoding="utf-8")
    (local_src / "sub" / "b.txt").write_text("BB", encoding="utf-8")

    try:
        print("[连通性]")
        result = adapter.test()
        check("test() 返回 ok", result.get("ok") is True, result.get("message", ""))

        print("\n[浏览]")
        root = adapter.list("/")
        check("根目录列出 user / tmp", sorted(e.name for e in root) == ["tmp", "user"], str([e.name for e in root]))
        check("根层条目都是目录", all(e.is_dir for e in root))

        warehouse = adapter.list("/user/hdfs/warehouse/ods")
        check("ods 下 2 个文件", len(warehouse) == 2, str([e.name for e in warehouse]))
        csv = next(e for e in warehouse if e.name == "t1.csv")
        check("文件 size 正确",
              csv.size == len(FS["/user/hdfs/warehouse/ods/t1.csv"]["content"]), str(csv.size))
        check("路径拼接正确", csv.path == "/user/hdfs/warehouse/ods/t1.csv", csv.path)

        st = adapter.stat("/user/hdfs/warehouse/ods")
        check("stat 识别目录", st.is_dir is True)

        print("\n[新建目录]")
        adapter.mkdir("/user/hdfs/warehouse/newdir")
        check("mkdir 建出目录", FS.get("/user/hdfs/warehouse/newdir", {}).get("type") == "DIRECTORY")
        check("mkdir 补齐中间层级", "/user/hdfs/warehouse/newdir" in FS)

        print("\n[上传（两段式：NameNode 307 → DataNode）]")
        payload = "你好，HDFS\n第二行\n".encode("utf-8")
        adapter.upload("/user/hdfs/warehouse/上传测试.txt", io.BytesIO(payload), len(payload))
        stored = FS.get("/user/hdfs/warehouse/上传测试.txt")
        check("两段式写入成功", stored is not None and stored["content"] == payload,
              f"{len(stored['content']) if stored else 0}B")
        check("上传后出现在列表中",
              "上传测试.txt" in [e.name for e in adapter.list("/user/hdfs/warehouse")])

        print("\n[下载 / 预览]")
        stream, size = adapter.open_read("/user/hdfs/warehouse/上传测试.txt")
        check("下载内容一致", b"".join(stream) == payload)

        head, _ = adapter.open_read("/user/hdfs/warehouse/ods/t1.csv", start=0, length=7)
        check("Range 读取生效", b"".join(head) == b"id,name", b"".join(head).decode())

        pv = adapter.preview("/user/hdfs/readme.txt", 4096)
        check("文本预览返回内容", pv["kind"] == "text" and "HDFS mock" in pv["text"])

        print("\n[预签名（WebHDFS 不支持，应报错）]")
        try:
            adapter.presign("/user/hdfs/readme.txt", 600)
            check("应拒绝预签名", False, "居然没报错")
        except Exception as exc:  # noqa: BLE001
            check("应拒绝预签名", "不支持" in str(exc), str(exc))
        check("supports_presign=False", adapter.supports_presign is False)

        print("\n[删除]")
        adapter.delete("/user/hdfs/warehouse/上传测试.txt", is_dir=False)
        check("单文件已删除", "/user/hdfs/warehouse/上传测试.txt" not in FS)

        adapter.delete("/user/hdfs/warehouse/ods", is_dir=True)
        check("目录递归删除", "/user/hdfs/warehouse/ods/t1.csv" not in FS)
        check("同级目录未受影响", "/user/hdfs/warehouse/newdir" in FS)

        print("\n[错误处理]")
        try:
            adapter.list("/user/hdfs/不存在")
            check("不存在路径应报错", False, "居然成功了")
        except Exception as exc:  # noqa: BLE001
            check("不存在路径给出中文提示", "路径不存在" in str(exc), str(exc))

        print("\n[接口层]")
        cfg.upsert_connection(conn)
        data = api.browse("selftest-hdfs", None)
        check("api.browse 默认落在根", data["path"] == "/" and data["parent"] is None, f"path={data['path']!r}")
        data = api.browse("selftest-hdfs", "/user/hdfs")
        check("api.browse parent 计算", data["parent"] == "/user", f"parent={data['parent']!r}")
        check("api 声明不支持预签名", data["supports_presign"] is False)

        print("\n[本地目录推到远端]")
        data = api.push_local("selftest-hdfs", [{"path": str(local_src), "is_dir": True}], "/user/hdfs/warehouse")
        check("本地目录推到 HDFS", "/user/hdfs/warehouse/pushed/a.txt" in FS, data["message"])
        check("嵌套子目录逐层建好", "/user/hdfs/warehouse/pushed/sub/b.txt" in FS)
        check("空目录也建出来", FS.get("/user/hdfs/warehouse/pushed/empty", {}).get("type") == "DIRECTORY")
        check("推送内容一致", FS["/user/hdfs/warehouse/pushed/sub/b.txt"]["content"] == b"BB")
        check("返回上传文件数", data.get("files") == 2, str(data.get("files")))

        print("\n[远端目录拉到本地]")
        back = local_root / "back"
        back.mkdir()
        data = api.pull_local("selftest-hdfs", [{"path": "/user/hdfs/warehouse/pushed", "is_dir": True}], str(back))
        got = {
            p.relative_to(back).as_posix(): p.read_bytes()
            for p in back.rglob("*") if p.is_file()
        }
        check("HDFS 目录拉到本地", got == {"pushed/a.txt": b"A", "pushed/sub/b.txt": b"BB"}, str(sorted(got)))
        check("空目录也建出来", (back / "pushed" / "empty").is_dir())
        check("返回下载文件数", data.get("files") == 2, str(data.get("files")))

        try:
            api.presign("selftest-hdfs", "/user/hdfs/readme.txt")
            check("接口层拒绝预签名", False, "居然没报错")
        except Exception as exc:  # noqa: BLE001
            check("接口层拒绝预签名", "不支持" in str(exc), str(exc))

    finally:
        try:
            cfg.delete_connection(conn["id"])
        except Exception:  # noqa: BLE001
            pass
        server.shutdown()
        shutil.rmtree(local_root, ignore_errors=True)

    print(f"\n{'=' * 56}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for item in FAIL:
            print("  -", item)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
