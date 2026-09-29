"""REST 接口层。

只负责「参数整形 → 调适配器 → 组织返回值」，不碰任何 HTTP 细节，
这样 server.py 保持极薄，接口逻辑也能脱离服务单独测试。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from . import __version__
from . import config as cfg
from .adapters import StoreAdapter, StoreError, build_adapter
from .adapters.base import join_path

PREVIEW_MAX_BYTES = 256 * 1024


class ApiError(Exception):
    """带 HTTP 状态码的业务异常。"""

    def __init__(self, message: str, status: int = 400, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _adapter(conn_id: str) -> StoreAdapter:
    if not conn_id:
        raise ApiError("缺少连接参数 conn")
    conn = cfg.find_connection(cfg.load_config(), conn_id)
    if conn is None:
        raise ApiError(f"连接不存在：{conn_id}", status=404)
    return build_adapter(conn)


def _is_masked(value: Any) -> bool:
    """前端回传的掩码占位值（含 *）不应覆盖真实凭据。"""
    text = str(value or "")
    return "*" in text


def _resolve_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把前端提交的表单整形成规整连接；掩码字段沿用已存凭据。"""
    conn = cfg.normalize_connection(payload)
    existing = cfg.find_connection(cfg.load_config(), conn["id"])
    if existing:
        for key in cfg.SECRET_FIELDS:
            if key not in payload or _is_masked(payload.get(key)):
                conn[key] = existing.get(key, "")
    return conn


def _invalidate_client(conn_id: str) -> None:
    """连接配置变更后丢弃缓存的 boto3 client。

    否则像「走系统代理」「校验 SSL」这类开关改了却仍复用旧连接池，
    表现为设置不生效。延迟导入是为了不让 boto3 拖慢启动。
    """
    from .adapters.s3 import invalidate_cache

    invalidate_cache(conn_id)


def _parent_of(path: str, root: str) -> str | None:
    """计算面包屑的上一级；None 表示已在顶层。"""
    current = path if path not in (None, "") else root
    if root == "":
        if not current:
            return None
        if "/" not in current:
            return ""
        return current.rsplit("/", 1)[0]
    clean = str(current or "/").rstrip("/")
    if clean in ("", "/"):
        return None
    if "/" not in clean[1:]:
        return "/"
    return clean.rsplit("/", 1)[0]


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
def get_meta() -> dict[str, Any]:
    conf = cfg.load_config()
    return {
        "app": cfg.APP_NAME,
        "version": __version__,
        "connection_types": cfg.CONNECTION_TYPES,
        "settings": conf.get("settings", {}),
        "config_file": str(cfg.config_file()),
        "config_dir": str(cfg.config_dir()),
    }


# --------------------------------------------------------------------------- #
# 连接管理
# --------------------------------------------------------------------------- #
def list_connections(reveal: str | None = None) -> dict[str, Any]:
    """列表默认对凭据打码；reveal 指定连接 id 时返回该条明文（编辑用）。"""
    conf = cfg.load_config()
    items: list[dict[str, Any]] = []
    for conn in conf.get("connections", []):
        spec = cfg.CONNECTION_TYPES.get(conn.get("type"), {})
        item = dict(conn) if reveal and conn.get("id") == reveal else cfg.mask_secret(conn)
        item["root_path"] = spec.get("root_path", "")
        item["supports_presign"] = spec.get("supports_presign", False)
        items.append(item)
    return {
        "connections": items,
        "config_file": str(cfg.config_file()),
        "config_dir": str(cfg.config_dir()),
    }


def save_connection(payload: dict[str, Any]) -> dict[str, Any]:
    conn = _resolve_payload(payload)
    missing = cfg.validate_connection(conn)
    if missing:
        raise ApiError("请补全：" + "、".join(missing))
    saved = cfg.upsert_connection(conn)
    _invalidate_client(saved["id"])
    return {"connection": cfg.mask_secret(saved), "message": f"已保存连接「{saved['name']}」"}


def delete_connection(conn_id: str) -> dict[str, Any]:
    if not cfg.delete_connection(conn_id):
        raise ApiError("连接不存在", status=404)
    _invalidate_client(conn_id)
    return {"message": "已删除连接"}


def test_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """测连通性。传 id 用已存配置，传完整表单则用表单值（支持未保存先测）。"""
    if payload.get("id") and not payload.get("endpoint"):
        adapter = _adapter(str(payload["id"]))
    else:
        conn = _resolve_payload(payload)
        missing = cfg.validate_connection(conn)
        if missing:
            raise ApiError("请补全：" + "、".join(missing))
        adapter = build_adapter(conn)

    result = adapter.test()
    result["connection"] = adapter.describe()
    return result


# --------------------------------------------------------------------------- #
# 浏览与操作
# --------------------------------------------------------------------------- #
def browse(conn_id: str, path: str | None) -> dict[str, Any]:
    adapter = _adapter(conn_id)
    root = adapter.root_path
    target = root if path is None else path
    if root == "/" and target == "":
        target = "/"

    entries = adapter.list(target)
    return {
        "path": target,
        "parent": _parent_of(target, root),
        "root_path": root,
        "entries": [e.to_dict() for e in entries],
        "supports_presign": adapter.supports_presign,
        "count": len(entries),
    }


def mkdir(conn_id: str, path: str) -> dict[str, Any]:
    if not path:
        raise ApiError("请填写目录名")
    adapter = _adapter(conn_id)
    adapter.mkdir(path)
    return {"message": f"已创建目录 {path}", "path": path}


def delete(conn_id: str, path: str, is_dir: bool) -> dict[str, Any]:
    if not path:
        raise ApiError("缺少要删除的路径")
    adapter = _adapter(conn_id)
    count = adapter.delete(path, is_dir)
    return {"message": f"已删除 {count} 个对象" if count > 1 else "已删除", "removed": count}


# --------------------------------------------------------------------------- #
# 复制 / 移动
# --------------------------------------------------------------------------- #
def _transfer_target(src: str, dest_dir: str) -> str:
    """算出目标路径，并挡掉「复制进自己里面」这类会滚雪球的用法。"""
    if not dest_dir:
        raise ApiError("请选择目标目录")
    name = str(src).rstrip("/").rsplit("/", 1)[-1]
    if not name:
        raise ApiError(f"路径无法定位条目：{src}")
    dest = join_path(dest_dir, name)
    source = str(src).rstrip("/")
    if dest.rstrip("/") == source:
        raise ApiError(f"目标与源相同：{dest}")
    if dest.rstrip("/").startswith(source + "/"):
        raise ApiError("不能把条目复制 / 移动到它自己里面", detail=f"源 {src} → 目标目录 {dest_dir}")
    return dest


def _transfer(conn_id: str, sources: list[dict[str, Any]], dest_dir: str, move: bool) -> dict[str, Any]:
    items = [(str(s.get("path") or ""), bool(s.get("is_dir"))) for s in sources or []]
    items = [(p, d) for p, d in items if p]
    if not items:
        raise ApiError("请先勾选要操作的条目")

    # 先整体校验再动手，避免「前几个移过去了、后几个才报错」的半截结果
    for path, _ in items:
        _transfer_target(path, dest_dir)

    adapter = _adapter(conn_id)
    count = 0
    for path, is_dir in items:
        count += adapter.move(path, dest_dir, is_dir) if move else adapter.copy(path, dest_dir, is_dir)

    action = "移动" if move else "复制"
    return {"message": f"已{action} {count} 个对象" if count > 1 else f"已{action}", "count": count}


def copy(conn_id: str, sources: list[dict[str, Any]], dest_dir: str) -> dict[str, Any]:
    return _transfer(conn_id, sources, dest_dir, move=False)


def move(conn_id: str, sources: list[dict[str, Any]], dest_dir: str) -> dict[str, Any]:
    return _transfer(conn_id, sources, dest_dir, move=True)


def preview(conn_id: str, path: str, max_bytes: int = PREVIEW_MAX_BYTES) -> dict[str, Any]:
    adapter = _adapter(conn_id)
    result = adapter.preview(path, max_bytes)
    result["path"] = path
    result["name"] = path.rstrip("/").rsplit("/", 1)[-1]
    return result


def presign(conn_id: str, path: str, expires: int = 3600) -> dict[str, Any]:
    adapter = _adapter(conn_id)
    if not adapter.supports_presign:
        raise ApiError("当前存储类型不支持生成预签名链接")
    expires = max(60, min(int(expires or 3600), 7 * 24 * 3600))
    return {"url": adapter.presign(path, expires), "expires": expires}


# --------------------------------------------------------------------------- #
# 上传进度任务（内存态，供前端轮询）
# --------------------------------------------------------------------------- #
class UploadTask:
    """一条上传批次的内存态进度任务。

    ThreadingHTTPServer 下「跑上传的线程」与「被轮询的线程」不是同一个，
    所有字段变更都走锁；任务只存在内存里、短期保留，不落盘。
    """

    __slots__ = (
        "task_id",
        "label",
        "stage",
        "files_total",
        "files_done",
        "current_file",
        "total_bytes",
        "transferred",
        "error",
        "_lock",
        "_updated_at",
    )

    def __init__(self, task_id: str, label: str = ""):
        self.task_id = task_id
        self.label = label
        self.stage = "pending"
        self.files_total = 0
        self.files_done = 0
        self.current_file = ""
        self.total_bytes = 0
        self.transferred = 0
        self.error = ""
        self._lock = threading.Lock()
        self._updated_at = time.time()

    def set(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self, key, value)
            self._updated_at = time.time()

    def add_bytes(self, amount: int) -> None:
        """追加一段已传输字节（适配器进度回调按增量调它）。"""
        with self._lock:
            self.transferred += amount
            self._updated_at = time.time()

    def file_done(self) -> None:
        with self._lock:
            self.files_done += 1
            self._updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "task_id": self.task_id,
                "label": self.label,
                "stage": self.stage,
                "files_total": self.files_total,
                "files_done": self.files_done,
                "current_file": self.current_file,
                "total_bytes": self.total_bytes,
                "transferred": self.transferred,
                "percent": round(self.transferred / self.total_bytes * 100, 1) if self.total_bytes else 0,
                "error": self.error,
            }


_TASKS: dict[str, UploadTask] = {}
_TASKS_LOCK = threading.Lock()
_TASK_TTL = 300.0  # 结束 5 分钟后清理，前端早就不轮询了


def new_task(task_id: str = "", label: str = "") -> UploadTask:
    """注册一条任务；task_id 为空时生成一个。"""
    tid = task_id or uuid.uuid4().hex
    task = UploadTask(tid, label)
    with _TASKS_LOCK:
        _TASKS[tid] = task
    return task


def get_task(task_id: str) -> UploadTask | None:
    with _TASKS_LOCK:
        return _TASKS.get(task_id)


def finish_task(task_id: str, error: str = "") -> None:
    task = get_task(task_id)
    if task:
        task.set(stage="error" if error else "done", error=error)
        _prune_tasks()


def task_snapshot(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if task is None:
        raise ApiError("上传任务不存在", status=404)
    return {"task": task.to_dict()}


def _prune_tasks() -> None:
    """清理早已结束的任务，防止内存无限增长。"""
    now = time.time()
    with _TASKS_LOCK:
        for tid in [
            t for t, task in _TASKS.items() if task.stage in ("done", "error") and now - task._updated_at > _TASK_TTL
        ]:
            del _TASKS[tid]


def upload(conn_id: str, path: str, stream: BinaryIO, size: int | None, task_id: str = "") -> dict[str, Any]:
    if not path:
        raise ApiError("缺少上传目标路径")
    adapter = _adapter(conn_id)
    task = get_task(task_id) if task_id else None
    if task:
        name = path.rstrip("/").rsplit("/", 1)[-1] or path
        task.set(
            stage="upload",
            current_file=name,
            files_total=1,
            files_done=0,
            total_bytes=size or 0,
            transferred=0,
            error="",
        )
    adapter.upload(path, stream, size, task.add_bytes if task else None)
    return {"message": f"已上传到 {path}", "path": path}


def open_download(conn_id: str, path: str) -> tuple[Iterator[bytes], int | None, str]:
    adapter = _adapter(conn_id)
    stream, size = adapter.open_read(path)
    filename = path.rstrip("/").rsplit("/", 1)[-1] or "download"
    return stream, size, filename


# --------------------------------------------------------------------------- #
# 本地目录（双栏里的另一栏）
# --------------------------------------------------------------------------- #
LOCAL_CHUNK = 1 << 20


def local_list(path: str) -> dict[str, Any]:
    """列出本地目录，结构与远端 browse 对齐，前端两栏共用渲染逻辑。"""
    if not path.strip():
        raise ApiError("请填写本地目录路径")
    try:
        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            raise ApiError(f"目录不存在：{target}")
        entries: list[dict[str, Any]] = []
        for child in target.iterdir():
            try:
                stat = child.stat()
            except OSError:
                continue  # 无权限 / 被占用的条目直接跳过
            is_dir = child.is_dir()
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_dir": is_dir,
                    "size": None if is_dir else stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes"),
                }
            )
    except ApiError:
        raise
    except OSError as exc:
        raise ApiError(f"无法读取目录：{path}", detail=str(exc)) from exc

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    parent = target.parent
    return {
        "path": str(target),
        "parent": str(parent) if parent != target else None,
        "entries": entries,
        "count": len(entries),
    }


def local_open_download(path: str) -> tuple[BinaryIO, int, str]:
    if not path.strip():
        raise ApiError("缺少本地文件路径")
    target = Path(path)
    if not target.is_file():
        raise ApiError(f"文件不存在：{target}")
    return target.open("rb"), target.stat().st_size, target.name


def local_upload(dir_path: str, name: str, stream: BinaryIO) -> dict[str, Any]:
    """把上传的字节流写到本地目录（目录来自本地栏当前位置，名字单独传，服务端拼路径）。"""
    if not dir_path.strip():
        raise ApiError("请先在本地栏打开一个目录")
    if not name.strip() or name.strip() in (".", ".."):
        raise ApiError("文件名不合法")
    parent = Path(dir_path)
    if not parent.is_dir():
        raise ApiError(f"本地目录不存在：{parent}")
    target = parent / name
    try:
        with target.open("wb") as fh:
            while True:
                chunk = stream.read(LOCAL_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
    except OSError as exc:
        raise ApiError(f"写入本地文件失败：{target}", detail=str(exc)) from exc
    return {"message": f"已保存到 {target}", "path": str(target)}


def _push_local_file(adapter: StoreAdapter, src: Path, dest: str, task: UploadTask | None = None) -> None:
    """把一个本地文件写进远端；大文件由适配层自己分片。

    task 非空时上报字节进度，并在成功后把已完成文件数 +1。
    """
    try:
        size = src.stat().st_size
        with src.open("rb") as fh:
            adapter.upload(dest, fh, size, task.add_bytes if task else None)
    except OSError as exc:
        raise ApiError(f"读取本地文件失败：{src}", detail=str(exc)) from exc
    else:
        if task:
            task.file_done()


def _push_local_dir(adapter: StoreAdapter, src: Path, dest_dir: str, task: UploadTask | None = None) -> int:
    """递归把本地目录推到远端 dest_dir 下（源作为同名子项进去），返回上传的文件数。"""

    def on_error(exc: OSError) -> None:
        # 不静默跳过读不了的子目录，否则用户会以为整棵树都传完了
        raise ApiError(f"读取本地目录失败：{exc.filename or src}", detail=str(exc))

    root = join_path(dest_dir, src.name)
    adapter.mkdir(root)  # 空目录也要在远端留下同名目录
    made = {root}
    count = 0
    for current, _dirs, files in os.walk(src, onerror=on_error):
        here = Path(current)
        rel = "" if here == src else here.relative_to(src).as_posix()
        base = join_path(root, rel) if rel else root
        if base not in made:
            adapter.mkdir(base)  # 逐层建子目录（HDFS 不会替我们建父目录）
            made.add(base)
        for name in sorted(files):
            if task:
                task.set(current_file=str(here / name))
            _push_local_file(adapter, here / name, join_path(base, name), task)
            count += 1
    return count


def _scan_push_sizes(items: list[tuple[str, bool]]) -> tuple[int, int]:
    """预扫本地文件 / 目录：返回 (文件数, 总字节数)，作为进度分母。

    与正式上传循环独立，统计不到就当 0 处理，不影响上传本身。
    """
    files_total = 0
    total_bytes = 0
    for raw, is_dir in items:
        src = Path(raw)
        if is_dir or src.is_dir():
            if not src.is_dir():
                continue  # 具体校验留给正式上传循环
            for current, _dirs, names in os.walk(src):
                for name in names:
                    try:
                        total_bytes += (Path(current) / name).stat().st_size
                    except OSError:
                        pass
                    files_total += 1
        elif src.is_file():
            files_total += 1
            try:
                total_bytes += src.stat().st_size
            except OSError:
                pass
    return files_total, total_bytes


def push_local(
    conn_id: str, sources: list[dict[str, Any]], dest_dir: str, task_id: str = "", label: str = ""
) -> dict[str, Any]:
    """把本地文件 / 目录推到远端目录下（目录递归）。

    与 ``copy`` / ``move`` 语义一致：``dest_dir`` 是远端父目录，源以同名子项落进去。
    数据由服务端直接读本地磁盘写进存储，不经过浏览器中转。
    ``task_id`` 非空时先预扫形成进度分母，再逐文件上报当前进度。
    """
    if not str(dest_dir or "").strip():
        raise ApiError("请先在左侧进入某个桶 / 目录再上传")
    items = [(str(s.get("path") or ""), bool(s.get("is_dir"))) for s in sources or []]
    items = [(path, is_dir) for path, is_dir in items if path]
    if not items:
        raise ApiError("请先选择要上传的本地文件 / 目录")

    task = get_task(task_id) if task_id else None
    if task:
        task.set(stage="scan", label=label or "")
        files_total, total_bytes = _scan_push_sizes(items)
        task.set(
            stage="upload",
            files_total=files_total,
            total_bytes=total_bytes,
            files_done=0,
            transferred=0,
            current_file="",
            error="",
        )

    adapter = _adapter(conn_id)
    count = 0
    for raw, is_dir in items:
        src = Path(raw)
        if task:
            task.set(current_file=src.name)
        if is_dir or src.is_dir():
            if not src.is_dir():
                raise ApiError(f"目录不存在：{src}")
            count += _push_local_dir(adapter, src, dest_dir, task)
        else:
            if not src.is_file():
                raise ApiError(f"文件不存在：{src}")
            _push_local_file(adapter, src, join_path(dest_dir, src.name), task)
            count += 1

    message = f"已上传 {count} 个文件到 {dest_dir}" if count else "已创建目录（没有文件）"
    return {"message": message, "files": count}


def _pull_local_file(adapter: StoreAdapter, src: str, target: Path) -> None:
    """把一个远端对象写到本地文件；先落 .part 再改名，失败不留半截文件。"""
    stream, _size = adapter.open_read(src)
    part = target.with_name(f"{target.name}.objstore-part")
    try:
        with part.open("wb") as fh:
            for chunk in stream:
                fh.write(chunk)
        os.replace(part, target)
    except OSError as exc:
        raise ApiError(f"写入本地文件失败：{target}", detail=str(exc)) from exc
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


def _pull_local_dir(adapter: StoreAdapter, src: str, target: Path) -> int:
    """递归把远端目录拉到本地 target 目录下，返回下载的文件数。"""
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApiError(f"创建本地目录失败：{target}", detail=str(exc)) from exc

    count = 0
    for entry in adapter.list(src):
        child = target / entry.name
        if entry.is_dir:
            count += _pull_local_dir(adapter, entry.path, child)
        else:
            _pull_local_file(adapter, entry.path, child)
            count += 1
    return count


def pull_local(conn_id: str, sources: list[dict[str, Any]], dest_dir: str) -> dict[str, Any]:
    """把远端文件 / 目录拉到本地目录下（目录递归）。

    与 ``push_local`` / ``copy`` 语义一致：``dest_dir`` 是本地父目录，源以同名子项落进去。
    数据由服务端直接从存储写到本地磁盘，不经过浏览器中转。
    """
    if not str(dest_dir or "").strip():
        raise ApiError("请先在右侧打开一个本地目录")
    dest = Path(str(dest_dir)).expanduser()
    if not dest.is_dir():
        raise ApiError(f"本地目录不存在：{dest}")

    items = [(str(s.get("path") or ""), bool(s.get("is_dir"))) for s in sources or []]
    items = [(path, is_dir) for path, is_dir in items if path]
    if not items:
        raise ApiError("请先选择要下载的远端条目")

    adapter = _adapter(conn_id)
    count = 0
    for path, is_dir in items:
        name = str(path).rstrip("/").rsplit("/", 1)[-1]
        if not name:
            raise ApiError(f"路径无法定位条目：{path}")
        target = dest / name
        if is_dir:
            count += _pull_local_dir(adapter, path, target)
        else:
            _pull_local_file(adapter, path, target)
            count += 1

    message = f"已下载 {count} 个文件到 {dest}" if count else "已创建目录（没有文件）"
    return {"message": message, "files": count}


# --------------------------------------------------------------------------- #
# 收藏夹（存配置文件，换浏览器 / 换机器 / 拷备份都能继承）
# --------------------------------------------------------------------------- #
def get_favorites() -> dict[str, Any]:
    conf = cfg.load_config()
    return {"favorites": cfg.normalize_favorites(conf.get("favorites"))}


def save_favorites(payload: dict[str, Any]) -> dict[str, Any]:
    """整份覆盖式保存（收藏夹很小，前端改完直接回传整份，逻辑最省）。"""
    if not isinstance(payload, dict) or not (
        isinstance(payload.get("remote"), dict) or isinstance(payload.get("local"), list)
    ):
        raise ApiError("收藏夹数据格式不对", detail="需要 {remote: {...}, local: [...]}")
    favs = cfg.normalize_favorites(payload)
    conf = cfg.load_config()
    conf["favorites"] = favs
    cfg.save_config(conf)
    return {"message": "收藏夹已保存", "favorites": favs}


# --------------------------------------------------------------------------- #
# 配置备份
# --------------------------------------------------------------------------- #
def export_backup() -> tuple[str, str]:
    return cfg.export_config()


def import_backup(text: str, merge: bool = True) -> dict[str, Any]:
    try:
        result = cfg.import_config(text, merge)
    except json.JSONDecodeError as exc:
        raise ApiError("备份文件不是合法 JSON", detail=str(exc)) from exc
    except ValueError as exc:
        raise ApiError(str(exc)) from exc

    stats = result.get("_import_stats") or {}
    message = "已导入备份"
    if stats:
        message = f"已导入备份：新增 {stats.get('added', 0)} 条，更新 {stats.get('updated', 0)} 条"
    return {"message": message, "count": len(result.get("connections", []))}
