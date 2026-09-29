"""S3 兼容对象存储适配器。

同一套实现覆盖 MinIO、阿里云 OSS、腾讯云 COS、AWS S3 —— 这些都是 S3 协议，
差异集中在 ``endpoint``、``region`` 和寻址方式（path / virtual host）上，
全部通过连接配置传入，代码无需分支。

路径约定：``mybucket/logs/2026/``，空串表示「桶列表」层。
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from typing import Any, BinaryIO, Iterator

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError

from . import register
from .base import Entry, StoreAdapter, StoreError, guess_preview_kind, join_path

# 超过该阈值走临时文件 + 分片上传，避免把大文件整个读进内存
MULTIPART_THRESHOLD = 32 * 1024 * 1024
MULTIPART_CHUNK = 8 * 1024 * 1024

# S3 服务端单次复制的对象上限，超过要改成分片复制（本工具不做）
COPY_SIZE_LIMIT = 5 * 1024 ** 3

# 客户端缓存：避免每次请求都重新加载服务模型（boto3 client 创建开销不小）
_CLIENT_CACHE: dict[tuple, Any] = {}


def _cache_key(conn: dict[str, Any]) -> tuple:
    """缓存键要覆盖所有会影响 client 行为的字段，否则改了开关不生效。"""
    return (
        conn.get("id"),
        conn.get("endpoint"),
        conn.get("access_key"),
        conn.get("secret_key"),
        conn.get("region"),
        conn.get("addressing_style"),
        conn.get("verify_ssl"),
        conn.get("use_proxy"),
    )


def _client(conn: dict[str, Any]):
    key = _cache_key(conn)
    cached = _CLIENT_CACHE.get(key)
    if cached is not None:
        return cached

    if not conn.get("endpoint"):
        raise StoreError("缺少 endpoint，无法连接 S3 兼容存储")
    if not conn.get("access_key") or not conn.get("secret_key"):
        raise StoreError("缺少 Access Key / Secret Key")

    try:
        client = boto3.client(
            "s3",
            endpoint_url=conn["endpoint"],
            aws_access_key_id=conn["access_key"],
            aws_secret_access_key=conn["secret_key"],
            region_name=conn.get("region") or "us-east-1",
            verify=bool(conn.get("verify_ssl", True)),
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": conn.get("addressing_style") or "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=120,
                # 内网对象存储默认直连：boto3 会继承 HTTP_PROXY/HTTPS_PROXY 环境变量，
                # 而代理通常到不了内网地址，症状是莫名的 502 或超时。
                # proxies={} 表示不走代理，None 表示沿用环境变量。
                proxies=None if conn.get("use_proxy") else {},
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成可展示错误
        raise StoreError("初始化 S3 客户端失败", str(exc)) from exc

    _CLIENT_CACHE[key] = client
    return client


def invalidate_cache(conn_id: str | None = None) -> None:
    if conn_id is None:
        _CLIENT_CACHE.clear()
        return
    for key in [k for k in _CLIENT_CACHE if k[0] == conn_id]:
        _CLIENT_CACHE.pop(key, None)


def _explain(exc: Exception, action: str) -> StoreError:
    """把 boto3 的异常翻译成能直接给用户看的中文说明。"""
    if isinstance(exc, NoCredentialsError):
        return StoreError("缺少访问凭据", "请检查 Access Key / Secret Key 是否填写")

    if isinstance(exc, EndpointConnectionError):
        return StoreError(
            f"{action}失败：无法连接到 endpoint",
            f"{exc}\n请确认：地址与端口可达、存储服务已启动；内网地址若被系统代理接管，反而会连不上。",
        )

    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        detail = str(exc.response.get("Error", {}).get("Message", "")) or str(exc)
        mapping = {
            "InvalidAccessKeyId": "Access Key 不存在或已失效",
            "SignatureDoesNotMatch": "Secret Key 不正确（签名校验失败）",
            "AccessDenied": "凭据有效但缺少该操作的权限",
            "NoSuchBucket": "桶不存在",
            "NoSuchKey": "对象不存在",
            "BucketAlreadyOwnedByYou": "桶已存在",
            "InvalidBucketName": "桶名不合法",
            "PermanentRedirect": "区域或 endpoint 配置不正确，请检查 Region",
            "IllegalLocationConstraintException": "Region 与 endpoint 不匹配",
            "RequestTimeTooSkewed": "本机时间与服务器时间偏差过大，请校准系统时间",
        }
        if code.isdigit():
            # 纯数字 code 说明是 HTTP 层返回的（网关 / 代理 / 反向代理），不是 S3 语义错误
            hint = f"存储端返回 HTTP {code}"
            if code in ("502", "503", "504"):
                hint += "，多半是中间的代理或网关拦截；内网地址请关掉「走系统代理」"
            return StoreError(f"{action}失败：{hint}", detail)
        return StoreError(f"{action}失败：{mapping.get(code, code or '未知错误')}", detail)

    if isinstance(exc, BotoCoreError):
        text = str(exc)
        lowered = text.lower()
        if "failed to connect" in lowered or "could not connect" in lowered or "timed out" in lowered:
            return StoreError(f"{action}失败：无法连接到 endpoint", text)
        if "SSL" in text or "certificate" in text.lower():
            return StoreError(f"{action}失败：SSL 证书校验不通过", "自签名证书可在连接配置里关闭「校验 SSL 证书」")
        return StoreError(f"{action}失败", text)

    return StoreError(f"{action}失败", str(exc))


def _fmt_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        try:
            return value.astimezone().isoformat(timespec="seconds")
        except (ValueError, OSError):
            return value.isoformat(timespec="seconds")
    return str(value)


def _split(path: str) -> tuple[str, str]:
    """把 ``bucket/key/...`` 拆成 (bucket, key)。"""
    cleaned = str(path or "").strip("/")
    if not cleaned:
        raise StoreError("路径为空，无法定位对象")
    parts = cleaned.split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    return bucket, key


@register("s3")
class S3Adapter(StoreAdapter):
    @property
    def supports_presign(self) -> bool:
        return True

    @property
    def root_path(self) -> str:
        return ""

    # ---- 连通性 ----
    def test(self) -> dict[str, Any]:
        client = _client(self.conn)
        try:
            resp = client.list_buckets()
            buckets = [b.get("Name") for b in resp.get("Buckets", [])]
            return {"ok": True, "message": f"连接成功，可见 {len(buckets)} 个桶", "buckets": buckets[:50]}
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            # 凭据对但无列桶权限时，仍然算连接成功
            if code in ("AccessDenied", "AllAccessDisabled"):
                return {"ok": True, "message": "凭据有效，但账号没有列举全部桶的权限", "buckets": []}
            raise _explain(exc, "连通性测试") from exc
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "连通性测试") from exc

    # ---- 浏览 ----
    def list(self, path: str) -> list[Entry]:
        client = _client(self.conn)
        cleaned = str(path or "").strip("/")

        # 根层：列出桶
        if not cleaned:
            try:
                resp = client.list_buckets()
            except Exception as exc:  # noqa: BLE001
                raise _explain(exc, "列出桶") from exc
            return [
                Entry(name=str(b.get("Name")), path=str(b.get("Name")), is_dir=True,
                      mtime=_fmt_time(b.get("CreationDate")))
                for b in resp.get("Buckets", [])
            ]

        bucket, prefix = _split(cleaned)
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        entries: list[Entry] = []
        paginator = client.get_paginator("list_objects_v2")
        try:
            pages = paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/", PaginationConfig={"PageSize": 1000})
            for page in pages:
                for item in page.get("CommonPrefixes", []):
                    full = str(item.get("Prefix", ""))
                    name = full[len(prefix):].rstrip("/")
                    if name:
                        entries.append(Entry(name=name, path=f"{bucket}/{full.rstrip('/')}", is_dir=True))
                for item in page.get("Contents", []):
                    key = str(item.get("Key", ""))
                    if key == prefix or key.endswith("/") and len(key) == len(prefix):
                        continue  # 跳过目录占位对象
                    name = key[len(prefix):]
                    if not name:
                        continue
                    entries.append(Entry(
                        name=name,
                        path=f"{bucket}/{key}",
                        is_dir=False,
                        size=int(item.get("Size") or 0),
                        mtime=_fmt_time(item.get("LastModified")),
                        etag=str(item.get("ETag", "")).strip('"') or None,
                        storage_class=item.get("StorageClass"),
                    ))
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "列出目录内容") from exc

        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def stat(self, path: str) -> Entry:
        client = _client(self.conn)
        bucket, key = _split(path)
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "读取对象信息") from exc
        return Entry(
            name=key.rsplit("/", 1)[-1] or key,
            path=f"{bucket}/{key}",
            is_dir=False,
            size=int(head.get("ContentLength") or 0),
            mtime=_fmt_time(head.get("LastModified")),
            etag=str(head.get("ETag", "")).strip('"') or None,
        )

    # ---- 写操作 ----
    def mkdir(self, path: str) -> None:
        client = _client(self.conn)
        bucket, key = _split(path)
        key = key.rstrip("/") + "/"
        if not key.strip("/"):
            raise StoreError("请在桶内创建目录")
        try:
            client.put_object(Bucket=bucket, Key=key, Body=b"")
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "创建目录") from exc

    def delete(self, path: str, is_dir: bool) -> int:
        client = _client(self.conn)
        bucket, key = _split(path)
        if not key:
            raise StoreError("这是桶本身，本工具不执行删桶操作，请到控制台处理")

        try:
            if not is_dir:
                client.delete_object(Bucket=bucket, Key=key)
                return 1

            prefix = key.rstrip("/") + "/"
            removed = 0
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": 1000}):
                keys = [{"Key": str(o.get("Key"))} for o in page.get("Contents", [])]
                while keys:
                    batch, keys = keys[:1000], keys[1000:]
                    client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                    removed += len(batch)
            return removed
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "删除") from exc

    # ---- 复制（S3 走服务端 copy_object，数据不经过本机）----
    def copy(self, src: str, dest_dir: str, is_dir: bool) -> int:
        client = _client(self.conn)
        src_bucket, src_key = _split(src)
        if not src_key:
            raise StoreError("桶本身不支持复制，请进入桶内操作")
        dest_bucket, dest_key = _split(join_path(dest_dir, src_key.rstrip("/").rsplit("/", 1)[-1]))
        if not dest_key:
            raise StoreError("请选择桶内的目标目录")

        if not is_dir:
            size = self._head_size(client, src_bucket, src_key)
            self._copy_object(client, src_bucket, src_key, dest_bucket, dest_key, size)
            return 1

        src_prefix = src_key.rstrip("/") + "/"
        dest_prefix = dest_key.rstrip("/") + "/"
        # 先把源目录下的 key 全部列出来再动手：边列边复制的话，
        # 万一目标落在源自己里面，会把刚复制出来的对象又当成源继续复制。
        items: list[tuple[str, int]] = []
        paginator = client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=src_bucket, Prefix=src_prefix, PaginationConfig={"PageSize": 1000}):
                for obj in page.get("Contents", []):
                    items.append((str(obj.get("Key")), int(obj.get("Size") or 0)))
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "列举待复制对象") from exc

        if not items:  # 只剩占位对象的空目录：目标侧也落成目录
            self.mkdir(f"{dest_bucket}/{dest_key}")
            return 0

        for key, size in items:
            self._copy_object(client, src_bucket, key, dest_bucket, dest_prefix + key[len(src_prefix):], size)
        return len(items)

    @staticmethod
    def _head_size(client, bucket: str, key: str) -> int:
        try:
            return int(client.head_object(Bucket=bucket, Key=key).get("ContentLength") or 0)
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "读取对象信息") from exc

    @staticmethod
    def _copy_object(client, src_bucket: str, src_key: str, dest_bucket: str, dest_key: str, size: int) -> None:
        if size > COPY_SIZE_LIMIT:
            raise StoreError(
                "复制失败：单个对象超过 5 GB",
                "S3 服务端单次复制有 5 GB 上限，更大的对象请下载后重新上传。",
            )
        try:
            client.copy_object(
                Bucket=dest_bucket,
                Key=dest_key,
                CopySource={"Bucket": src_bucket, "Key": src_key},
            )
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "复制") from exc

    # ---- 读写 ----
    def open_read(self, path: str, start: int | None = None, length: int | None = None):
        client = _client(self.conn)
        bucket, key = _split(path)
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if start is not None or length is not None:
            if length is not None:
                kwargs["Range"] = f"bytes={start or 0}-{(start or 0) + length - 1}"
            else:
                kwargs["Range"] = f"bytes={start or 0}-"

        try:
            resp = client.get_object(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "读取对象") from exc

        body = resp["Body"]
        total = resp.get("ContentLength")
        if "ContentRange" in resp:
            total = int(str(resp["ContentRange"]).rsplit("/", 1)[-1])

        def iterator() -> Iterator[bytes]:
            try:
                for chunk in body.iter_chunks(chunk_size=256 * 1024):
                    yield chunk
            finally:
                body.close()

        return iterator(), (int(total) if total is not None else None)

    def upload(self, path: str, stream: BinaryIO, size: int | None) -> None:
        client = _client(self.conn)
        bucket, key = _split(path)
        if not key:
            raise StoreError("请指定要上传到的对象路径（不能直接上传到桶根）")

        try:
            if size is not None and size > MULTIPART_THRESHOLD:
                self._upload_large(client, bucket, key, stream, size)
            else:
                payload = stream.read() if size is None else stream.read(size)
                client.put_object(Bucket=bucket, Key=key, Body=payload)
        except StoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "上传") from exc

    @staticmethod
    def _upload_large(client, bucket: str, key: str, stream: BinaryIO, size: int) -> None:
        """大文件落临时文件后走分片上传，避免内存被打满。"""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".upload") as tmp:
                tmp_path = tmp.name
                remaining = size
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)

            from boto3.s3.transfer import TransferConfig

            with open(tmp_path, "rb") as fh:
                client.upload_fileobj(
                    fh, bucket, key,
                    Config=TransferConfig(
                        multipart_threshold=MULTIPART_THRESHOLD,
                        multipart_chunksize=MULTIPART_CHUNK,
                        max_concurrency=4,
                    ),
                )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def presign(self, path: str, expires: int = 3600) -> str:
        client = _client(self.conn)
        bucket, key = _split(path)
        if not key:
            raise StoreError("桶本身无法生成预签名链接")
        try:
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=int(expires),
            )
        except Exception as exc:  # noqa: BLE001
            raise _explain(exc, "生成预签名链接") from exc

    # ---- 预览 ----
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
