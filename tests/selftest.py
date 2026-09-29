"""本地自测：起一个极简 S3 兼容 mock，跑通浏览 / 上传 / 下载 / 预览 / 删除全链路。

    uv run python tests/selftest.py

mock 只实现 boto3 实际会用到的那几个动作（ListBuckets / ListObjectsV2 /
PutObject / GetObject / HeadObject / DeleteObject / DeleteObjects），
目的不是实现 S3，而是验证本工具的适配层与接口层接得上真实协议。
"""

from __future__ import annotations

import re
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

NS = "http://s3.amazonaws.com/doc/2006-03-01/"

# 内存里的假桶：key -> (内容, 修改时间)
STORE: dict[str, dict[str, tuple[bytes, str]]] = {
    "demo": {
        "readme.txt": (b"# demo bucket\nhello objstore\n", "2026-09-01T00:00:00.000Z"),
        "logs/2026/09/a.csv": (b"id,name\n1,alpha\n2,beta\n", "2026-09-10T08:00:00.000Z"),
        "logs/2026/09/b.log": (b"2026-09-11 INFO pipeline finished\n", "2026-09-11T09:30:00.000Z"),
        "images/pic.png": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "2026-09-02T00:00:00.000Z"),
    },
    "empty-bucket": {},
}


def _xml(body: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}'.encode("utf-8")


class MockS3(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102
        pass

    def parse_request(self) -> bool:
        """统一解码路径。

        boto3 会把中文 / 空格 key 发成百分号编码，而 BaseHTTPRequestHandler
        不会自动解码；不解码的话 mock 里存的 key 与断言用的 key 对不上。
        只解码 path 段，query 留给 parse_qs 处理，避免重复解码。
        """
        ok = super().parse_request()
        if not ok:
            return ok
        path, sep, query = self.path.partition("?")
        self.path = urllib.parse.unquote(path) + (sep + query if sep else "")
        return ok

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        raw = parsed.path.lstrip("/")

        if not raw:
            return self._list_buckets()
        bucket, _, key = raw.partition("/")
        if "list-type" in query:
            return self._list_objects(bucket, query)
        return self._get_object(bucket, key)

    def do_HEAD(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        bucket, _, key = parsed.path.lstrip("/").partition("/")
        item = STORE.get(bucket, {}).get(key)
        if item is None:
            return self._error(404, "NoSuchKey")
        body, mtime = item
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"mock-etag"')
        self.send_header("Last-Modified", "Mon, 01 Sep 2026 00:00:00 GMT")
        self.end_headers()

    # ---- PUT ----
    def do_PUT(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        bucket, _, key = parsed.path.lstrip("/").partition("/")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if bucket not in STORE:
            STORE[bucket] = {}

        # 服务端复制：x-amz-copy-source 形如 /srcbucket/srckey（boto3 会做百分号编码）
        copy_source = self.headers.get("x-amz-copy-source")
        if copy_source:
            src_bucket, _, src_key = urllib.parse.unquote(copy_source.lstrip("/")).partition("/")
            item = STORE.get(src_bucket, {}).get(src_key)
            if item is None:
                return self._error(404, "NoSuchKey")
            STORE[bucket][key] = item
            return self._send_xml('<CopyObjectResult><ETag>"mock-etag"</ETag></CopyObjectResult>')

        if key:
            STORE[bucket][key] = (body, "2026-09-16T12:00:00.000Z")

        self.send_response(200)
        self.send_header("ETag", '"mock-etag"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- DELETE ----
    def do_DELETE(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        bucket, _, key = parsed.path.lstrip("/").partition("/")
        STORE.get(bucket, {}).pop(key, None)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- POST（批量删除） ----
    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        # keep_blank_values：批量删除的 ?delete 是无值参数，默认会被丢弃
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        bucket = parsed.path.lstrip("/").split("/")[0]

        if "delete" in query:
            # 按提交进来的 Key 逐个删（跟真实 S3 一致），早先「清空整桶」太糊弄
            keys = re.findall(r"<Key>(.*?)</Key>", raw.decode("utf-8", errors="replace"))
            removed = [k for k in keys if STORE.get(bucket, {}).pop(k, None) is not None]
            body = "<DeleteResult>" + "".join(
                f"<Deleted><Key>{k}</Key></Deleted>" for k in removed
            ) + "</DeleteResult>"
            return self._send_xml(body)

        self._error(400, "NotImplemented")

    # ---- 各动作 ----
    def _list_buckets(self):
        items = "".join(
            f"<Bucket><Name>{name}</Name>"
            f"<CreationDate>2026-09-01T00:00:00.000Z</CreationDate></Bucket>"
            for name in STORE
        )
        self._send_xml(
            f"<ListAllMyBucketsResult xmlns=\"{NS}\">"
            f"<Owner><ID>mock</ID><DisplayName>mock</DisplayName></Owner>"
            f"<Buckets>{items}</Buckets></ListAllMyBucketsResult>"
        )

    def _list_objects(self, bucket: str, query: dict):
        prefix = (query.get("prefix") or [""])[0]
        delimiter = (query.get("delimiter") or [""])[0]
        keys = sorted(STORE.get(bucket, {}))

        contents = []
        common = set()
        for key in keys:
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                common.add(prefix + rest.split(delimiter)[0] + delimiter)
                continue
            body, mtime = STORE[bucket][key]
            contents.append(
                f"<Contents><Key>{key}</Key><LastModified>{mtime}</LastModified>"
                f'<ETag>"mock-etag"</ETag><Size>{len(body)}</Size>'
                f"<StorageClass>STANDARD</StorageClass></Contents>"
            )

        prefix_xml = "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in sorted(common))
        self._send_xml(
            f"<ListBucketResult xmlns=\"{NS}\">"
            f"<Name>{bucket}</Name><Prefix>{prefix}</Prefix><Delimiter>{delimiter}</Delimiter>"
            f"<MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>"
            f"{''.join(contents)}{prefix_xml}</ListBucketResult>"
        )

    def _get_object(self, bucket: str, key: str):
        item = STORE.get(bucket, {}).get(key)
        if item is None:
            return self._error(404, "NoSuchKey")
        body, _ = item
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"mock-etag"')
        self.send_header("Last-Modified", "Mon, 01 Sep 2026 00:00:00 GMT")
        self.end_headers()
        self.wfile.write(body)

    # ---- 工具 ----
    def _send_xml(self, body: str):
        data = _xml(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, s3_code: str):
        body = _xml(f"<Error><Code>{s3_code}</Code><Message>{s3_code}</Message></Error>")
        self.send_response(code)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_mock() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockS3)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


PASS, FAIL = [], []


def check(label: str, condition: bool, extra: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   {extra}" if extra else ""))


def main() -> int:
    server, endpoint = start_mock()
    print(f"mock S3 已启动：{endpoint}\n")

    conn = {
        "id": "selftest-s3",
        "type": "s3",
        "name": "自测 mock",
        "endpoint": endpoint,
        "access_key": "mock",
        "secret_key": "mock",
        "region": "us-east-1",
        "addressing_style": "path",
        "verify_ssl": True,
        "use_proxy": False,
    }
    adapter = build_adapter(conn)

    # 「本地目录 → 远端」用例的源目录树（放到临时目录，跑完删）
    local_root = Path(tempfile.mkdtemp(prefix="objstore-push-"))
    local_src = local_root / "pushed"
    (local_src / "sub" / "deep").mkdir(parents=True)
    (local_src / "empty").mkdir()
    (local_src / "a.txt").write_text("A", encoding="utf-8")
    (local_src / "sub" / "b.txt").write_text("BB", encoding="utf-8")
    (local_src / "sub" / "deep" / "c.txt").write_text("CCC", encoding="utf-8")

    try:
        print("[连通性]")
        result = adapter.test()
        check("test() 返回 ok", result.get("ok") is True, result.get("message", ""))
        check("可见 2 个桶", len(result.get("buckets", [])) == 2, str(result.get("buckets")))

        print("\n[浏览]")
        buckets = adapter.list("")
        check("根层列出桶", sorted(e.name for e in buckets) == ["demo", "empty-bucket"])
        check("根层条目都是目录", all(e.is_dir for e in buckets))

        entries = adapter.list("demo")
        names = [e.name for e in entries]
        check("桶内目录在前", entries[0].is_dir and entries[0].name == "images")
        check("识别出 3 个目录", sorted(n for n in names if n in ("images", "logs")) == ["images", "logs"])
        check("识别出 readme.txt", "readme.txt" in names)

        deeper = adapter.list("demo/logs/2026/09")
        check("深层目录能列出 2 个文件", len(deeper) == 2, str([e.name for e in deeper]))
        csv = next(e for e in deeper if e.name == "a.csv")
        check("文件 size 正确", csv.size == len(STORE["demo"]["logs/2026/09/a.csv"][0]), str(csv.size))

        print("\n[新建目录]")
        adapter.mkdir("demo/newdir")
        check("mkdir 落成占位对象", "newdir/" in STORE["demo"])
        check("新建目录出现在列表中", "newdir" in [e.name for e in adapter.list("demo")])

        print("\n[上传 / 下载]")
        payload = "你好，objstore\nline2\n".encode("utf-8")
        import io

        adapter.upload("demo/newdir/上传测试.txt", io.BytesIO(payload), len(payload))
        check("上传后对象存在", "newdir/上传测试.txt" in STORE["demo"])

        stream, size = adapter.open_read("demo/newdir/上传测试.txt")
        got = b"".join(stream)
        check("下载内容一致", got == payload, f"{len(got)}B")
        check("返回长度正确", size == len(payload), str(size))

        print("\n[读取信息 / 预览]")
        st = adapter.stat("demo/readme.txt")
        check("stat 大小正确", st.size == len(STORE["demo"]["readme.txt"][0]), str(st.size))

        pv = adapter.preview("demo/readme.txt", 4096)
        check("文本预览返回内容", pv["kind"] == "text" and "hello objstore" in pv["text"])
        img = adapter.preview("demo/images/pic.png", 4096)
        check("图片走 raw_url", img["kind"] == "image" and img["raw_url"].startswith("/api/raw"))

        print("\n[预签名]")
        url = adapter.presign("demo/readme.txt", 600)
        check("生成预签名链接", url.startswith(endpoint) and "X-Amz-Signature" in url, url[:70] + "…")

        print("\n[删除]")
        adapter.delete("demo/readme.txt", is_dir=False)
        check("单文件已删除", "readme.txt" not in STORE["demo"])

        removed = adapter.delete("demo/logs", is_dir=True)
        check("目录递归删除", "logs/2026/09/a.csv" not in STORE["demo"], f"removed={removed}")

        print("\n[复制 / 移动]")
        count = adapter.copy("demo/newdir", "demo/backup", is_dir=True)
        check("目录复制到另一个目录", "backup/newdir/上传测试.txt" in STORE["demo"], f"count={count}")
        check("复制出来的内容一致", STORE["demo"].get("backup/newdir/上传测试.txt", (b"", ""))[0] == payload)

        count = adapter.move("demo/newdir", "demo/moved", is_dir=True)
        check("目录移动后源侧没了", "newdir/上传测试.txt" not in STORE["demo"], f"count={count}")
        check("目录移动后目标在", "moved/newdir/上传测试.txt" in STORE["demo"])

        adapter.copy("demo/images/pic.png", "empty-bucket", is_dir=False)
        check("单文件跨桶复制", "pic.png" in STORE["empty-bucket"])

        adapter.move("empty-bucket/pic.png", "empty-bucket/sub", is_dir=False)
        check("单文件移动", "pic.png" not in STORE["empty-bucket"] and "sub/pic.png" in STORE["empty-bucket"])

        print("\n[接口层]")
        cfg.upsert_connection(conn)
        data = api.browse("selftest-s3", "")
        check("api.browse 根层", data["path"] == "" and data["parent"] is None, f"count={data['count']}")
        data = api.browse("selftest-s3", "demo")
        check("api.browse 桶内 parent 正确", data["parent"] == "", f"parent={data['parent']!r}")
        data = api.browse("selftest-s3", "demo/newdir")
        check("api.browse 深层 parent", data["parent"] == "demo", f"parent={data['parent']!r}")
        check("api 声明支持预签名", data["supports_presign"] is True)

        meta = api.get_meta()
        check("连接类型含 use_proxy 字段",
              any(f["key"] == "use_proxy" for f in meta["connection_types"]["s3"]["fields"]))

        try:
            api.copy("selftest-s3", [{"path": "demo/moved", "is_dir": True}], "demo/moved/inner")
            check("复制进自己里面被挡下", False)
        except api.ApiError as exc:
            check("复制进自己里面被挡下", "自己里面" in str(exc.message), exc.message)

        try:
            api.move("selftest-s3", [{"path": "demo/moved", "is_dir": True}], "demo")
            check("原地移动被挡下", False)
        except api.ApiError as exc:
            check("原地移动被挡下", "目标与源相同" in str(exc.message), exc.message)

        print("\n[本地目录推到远端]")
        data = api.push_local("selftest-s3", [{"path": str(local_src), "is_dir": True}], "demo")
        check("本地目录推到远端", "pushed/a.txt" in STORE["demo"], data["message"])
        check("嵌套子目录一起推上去", "pushed/sub/deep/c.txt" in STORE["demo"])
        check("空目录在远端留下同名目录", "pushed/empty/" in STORE["demo"])
        check("推送的内容一致", STORE["demo"].get("pushed/sub/b.txt", (b"", ""))[0] == b"BB")
        check("返回上传文件数", data.get("files") == 3, str(data.get("files")))

        api.push_local("selftest-s3", [{"path": str(local_src / "a.txt"), "is_dir": False}], "demo/moved")
        check("单文件推到指定目录", "moved/a.txt" in STORE["demo"])

        api.push_local(
            "selftest-s3",
            [{"path": str(local_src / "a.txt"), "is_dir": False},
             {"path": str(local_src / "sub" / "b.txt"), "is_dir": False}],
            "demo/multi",
        )
        check("多选一次推多个文件", "multi/a.txt" in STORE["demo"] and "multi/b.txt" in STORE["demo"])

        try:
            api.push_local("selftest-s3", [{"path": str(local_root / "不存在"), "is_dir": True}], "demo")
            check("本地路径不存在被挡下", False)
        except api.ApiError as exc:
            check("本地路径不存在被挡下", "不存在" in str(exc.message), exc.message)

        try:
            api.push_local("selftest-s3", [{"path": str(local_src), "is_dir": True}], "")
            check("目标目录为空被挡下", False)
        except api.ApiError as exc:
            check("目标目录为空被挡下", "桶 / 目录" in str(exc.message), exc.message)

        print("\n[远端目录拉到本地]")
        back = local_root / "back"
        back.mkdir()
        data = api.pull_local("selftest-s3", [{"path": "demo/pushed", "is_dir": True}], str(back))
        got = {
            p.relative_to(back).as_posix(): p.read_bytes()
            for p in back.rglob("*") if p.is_file()
        }
        want = {
            "pushed/a.txt": b"A",
            "pushed/sub/b.txt": b"BB",
            "pushed/sub/deep/c.txt": b"CCC",
        }
        check("远端目录拉到本地（含嵌套）", got == want, str(sorted(got)))
        check("远端空目录也建出来", (back / "pushed" / "empty").is_dir())
        check("返回下载文件数", data.get("files") == 3, str(data.get("files")))

        api.pull_local("selftest-s3", [{"path": "demo/pushed/a.txt", "is_dir": False}], str(back))
        check("单文件拉到本地", (back / "a.txt").read_bytes() == b"A")

        try:
            api.pull_local("selftest-s3", [{"path": "demo/pushed", "is_dir": True}], str(local_root / "不存在"))
            check("本地目标不存在被挡下", False)
        except api.ApiError as exc:
            check("本地目标不存在被挡下", "本地目录不存在" in str(exc.message), exc.message)

        print("\n[收藏夹（存配置文件）]")
        api.save_favorites({
            "remote": {"selftest-s3": [{"name": "demo", "path": "demo"},
                                       {"name": "logs", "path": "demo/logs"}]},
            "local": [{"name": "temp", "path": str(local_root)}],
        })
        favs = api.get_favorites()["favorites"]
        check("收藏夹写读一致", favs["remote"]["selftest-s3"][1]["path"] == "demo/logs", str(favs))
        check("本地收藏也在", favs["local"][0]["path"] == str(local_root), str(favs))

        api.save_favorites({"remote": {"selftest-s3": [{"path": "demo"}, {"path": "demo"}]}, "local": []})
        favs = api.get_favorites()["favorites"]
        check("重复 path 去重", len(favs["remote"]["selftest-s3"]) == 1, str(favs))
        check("缺名字时取路径末段", favs["remote"]["selftest-s3"][0]["name"] == "demo", str(favs))

        try:
            api.save_favorites({"nope": 1})
            check("非法收藏夹被挡下", False)
        except api.ApiError as exc:
            check("非法收藏夹被挡下", "格式" in exc.message, exc.message)

        backup_text = api.export_backup()[1]
        check("导出备份带上收藏夹", '"favorites"' in backup_text)
        cfg.save_config({**cfg.load_config(), "favorites": {"remote": {}, "local": []}})
        api.import_backup(backup_text, merge=True)
        favs = api.get_favorites()["favorites"]
        check("导入备份把收藏夹带回来", favs["remote"].get("selftest-s3", [{}])[0].get("path") == "demo", str(favs))

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
