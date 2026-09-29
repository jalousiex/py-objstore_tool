"""配置管理。

设计要点（对齐需求）：
1. 本地存储：配置固定落在程序同目录的 ``.config/connections.json``
   （点前缀目录默认被仓库忽略，凭据不进版本库）。
2. 独立运行：只用标准库，不依赖外部服务或数据库。
3. 方便备份：全部配置集中在一个目录、一份 JSON 文件里，整目录拷走即完成备份。
   凭据按明文存储，保证备份到另一台机器后可直接使用。

打包成 exe 后，``app_dir()`` 指向 exe 所在目录，因此把 exe 与 .config 目录
一起放进 U 盘或任意文件夹即可携带使用。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from . import APP_NAME

CONFIG_VERSION = 1
CONFIG_DIR_NAME = ".config"
CONFIG_FILE_NAME = "connections.json"
# 历史：配置曾放在 ``config/``，因含明文凭据不宜入库，改为点前缀目录（被 .gitignore 的 .*/ 忽略）。
LEGACY_CONFIG_DIR_NAME = "config"

# 明文存储的凭据字段名，仅用于前端掩码展示，不做加密。
SECRET_FIELDS = ("secret_key",)

# 收藏夹默认值：远端按连接 id 分组（同一连接下多套收藏），本地是扁平列表。
# 放配置文件里（而不是浏览器 localStorage）是为了换浏览器 / 换机器 / 拷备份都能继承。
DEFAULT_FAVORITES: dict[str, Any] = {"remote": {}, "local": []}

DEFAULT_SETTINGS: dict[str, Any] = {
    # 监听端口，被占用时自动向后探测。仅绑定 127.0.0.1，不对外暴露。
    "port": 8765,
    # 启动后自动打开浏览器
    "open_browser": True,
}

# 前端据此动态渲染连接表单，新增存储类型时只改这里。
CONNECTION_TYPES: dict[str, dict[str, Any]] = {
    "s3": {
        "label": "S3 兼容对象存储",
        "hint": "MinIO / 阿里云 OSS / 腾讯云 COS / AWS S3 均可，填对 endpoint 即可",
        "root_path": "",
        "supports_presign": True,
        "fields": [
            {"key": "name", "label": "连接名称", "type": "text", "required": True,
             "placeholder": "例如 内网 MinIO"},
            {"key": "endpoint", "label": "Endpoint", "type": "text", "required": True,
             "placeholder": "http://minio.internal:9000"},
            {"key": "access_key", "label": "Access Key", "type": "text", "required": True,
             "placeholder": "AKIA... / minioadmin"},
            {"key": "secret_key", "label": "Secret Key", "type": "password", "required": True,
             "placeholder": "明文保存在本地配置中"},
            {"key": "region", "label": "Region", "type": "text", "required": False,
             "placeholder": "默认 us-east-1", "default": "us-east-1"},
            {"key": "addressing_style", "label": "寻址方式", "type": "select", "required": False,
             "options": [
                 {"value": "path", "label": "Path style（MinIO/自建推荐）"},
                 {"value": "virtual", "label": "Virtual host style（云厂商常用）"},
             ],
             "default": "path"},
            {"key": "use_proxy", "label": "走系统代理", "type": "bool",
             "required": False, "default": False},
            {"key": "verify_ssl", "label": "校验 SSL 证书", "type": "bool",
             "required": False, "default": True},
        ],
    },
    "webhdfs": {
        "label": "HDFS / WebHDFS",
        "hint": "Hadoop 3 默认 9870 端口，Hadoop 2 为 50070",
        "root_path": "/",
        "supports_presign": False,
        "fields": [
            {"key": "name", "label": "连接名称", "type": "text", "required": True,
             "placeholder": "例如 集群 HDFS"},
            {"key": "endpoint", "label": "NameNode 地址", "type": "text", "required": True,
             "placeholder": "http://namenode.internal:9870"},
            {"key": "user", "label": "用户名 (user.name)", "type": "text", "required": False,
             "placeholder": "默认 hdfs", "default": "hdfs"},
        ],
    },
}


def app_dir() -> Path:
    """配置根目录，按运行形态定位（三种形态都要能找到自己的 config/）：

    1. 打包成 exe（PyInstaller）→ exe 同目录，便携；
    2. 源码 / editable 运行（uv run）→ 项目根目录，配置跟着项目走；
    3. uv tool 安装运行（uv tool install）→ 用户目录 ``%LOCALAPPDATA%\\objstore_tool``：
       此时包位于 site-packages 内，不能把配置写进环境内部（升级 / 重装即丢）。

    环境变量 ``OBJSTORE_CONFIG_DIR`` 优先级最高，可强制指定，
    用来让「安装运行」复用项目里的同一份配置。
    """
    override = os.environ.get("OBJSTORE_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    project_root = Path(__file__).resolve().parent.parent
    if (project_root / "pyproject.toml").is_file():
        return project_root

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME") or Path.home()
    return Path(base) / APP_NAME


def resource_dir() -> Path:
    """前端静态资源目录。

    ``web/`` 随包分发（``objstore_tool/web/``），所以源码运行与 uv 临时 / 安装运行
    三种形态都取包内那一份；打包成 exe 后走 PyInstaller 的解压目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "web"
    return Path(__file__).resolve().parent / "web"


def _migrate_legacy_dir(target: Path) -> None:
    """把旧的 ``config/connections.json`` 迁到 ``.config/``（整体改名，不复制凭据）。

    仅在目标不存在且旧文件存在时执行一次；旧目录迁空后顺手删掉。
    """
    legacy = app_dir() / LEGACY_CONFIG_DIR_NAME
    legacy_file = legacy / CONFIG_FILE_NAME
    if target.exists() or not legacy_file.is_file():
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        os.replace(legacy_file, target / CONFIG_FILE_NAME)
        try:
            legacy.rmdir()
        except OSError:
            pass  # 目录里还有别的东西，留着不动
    except OSError:
        pass


def config_dir() -> Path:
    d = app_dir() / CONFIG_DIR_NAME
    _migrate_legacy_dir(d)
    return d


def config_file() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def _default_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "app": APP_NAME,
        "settings": deepcopy(DEFAULT_SETTINGS),
        "connections": [],
        "favorites": deepcopy(DEFAULT_FAVORITES),
    }


def _clean_fav_items(items: Any) -> list[dict[str, str]]:
    """收藏条目整形：丢掉没有 path 的、按 path 去重、名字缺省时取路径末段。"""
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        name = str(item.get("name") or "").strip()
        if not name:
            name = path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or path
        out.append({"name": name, "path": path})
    return out


def normalize_favorites(raw: Any) -> dict[str, Any]:
    """收藏夹整形；任何不合法输入都退化成「空收藏夹」，不抛异常。"""
    if not isinstance(raw, dict):
        return deepcopy(DEFAULT_FAVORITES)

    remote: dict[str, list[dict[str, str]]] = {}
    remote_raw = raw.get("remote")
    if isinstance(remote_raw, dict):
        for conn_id, items in remote_raw.items():
            cleaned = _clean_fav_items(items)
            if cleaned and str(conn_id).strip():
                remote[str(conn_id)] = cleaned
    return {"remote": remote, "local": _clean_fav_items(raw.get("local"))}


def _atomic_write(path: Path, text: str) -> None:
    """原子写入：先写临时文件再替换，避免中途失败损坏配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict[str, Any]:
    """读取配置；不存在或损坏时回退到默认配置。"""
    path = config_file()
    if not path.exists():
        cfg = _default_config()
        save_config(cfg)
        return cfg

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        backup = path.with_suffix(".broken.json")
        try:
            path.replace(backup)
        except OSError:
            pass
        cfg = _default_config()
        save_config(cfg)
        cfg["_recovered_from"] = str(backup)
        return cfg

    if not isinstance(raw, dict):
        raw = _default_config()

    raw.setdefault("version", CONFIG_VERSION)
    raw.setdefault("app", APP_NAME)
    settings = raw.get("settings")
    raw["settings"] = {**DEFAULT_SETTINGS, **(settings if isinstance(settings, dict) else {})}
    conns = raw.get("connections")
    if not isinstance(conns, list):
        conns = []
    original = [c for c in conns if isinstance(c, dict)]
    normalized = _with_unique_ids([normalize_connection(c) for c in original])
    raw["connections"] = normalized
    favorites_before = raw.get("favorites")
    raw["favorites"] = normalize_favorites(favorites_before)
    # 直接手改配置文件（比如新连接留空 id、复制粘贴出重复 id、收藏条目写坏）后，补齐的结果要写回磁盘：
    # 否则每次 load 都会重新随机生成 id，页面里对该连接的任何操作（浏览/编辑）都会因
    # id 对不上而失效。仅在内容确实变了时写，避免每次启动都动文件。
    if normalized != original or raw["favorites"] != favorites_before:
        save_config(raw)
    return raw


def save_config(cfg: dict[str, Any]) -> None:
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    cfg["version"] = CONFIG_VERSION
    cfg["app"] = APP_NAME
    cfg.setdefault("settings", deepcopy(DEFAULT_SETTINGS))
    cfg.setdefault("connections", [])
    cfg.setdefault("favorites", deepcopy(DEFAULT_FAVORITES))
    _atomic_write(config_file(), json.dumps(cfg, ensure_ascii=False, indent=2))


def _with_unique_ids(conns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保证 id 唯一：手改文件复制粘贴出的重复 id，只给后出现的重新生成。"""
    seen: set[str] = set()
    for conn in conns:
        if conn["id"] in seen:
            conn["id"] = uuid.uuid4().hex[:12]
        seen.add(conn["id"])
    return conns


def normalize_connection(data: dict[str, Any]) -> dict[str, Any]:
    """补齐字段、清理类型，保证适配层拿到的是规整结构。"""
    conn = dict(data)
    conn["id"] = str(conn.get("id") or uuid.uuid4().hex[:12])
    conn["type"] = str(conn.get("type") or "s3")
    conn["name"] = str(conn.get("name") or "未命名连接").strip()
    conn["endpoint"] = str(conn.get("endpoint") or "").strip().rstrip("/")
    conn["region"] = str(conn.get("region") or "us-east-1")
    conn["user"] = str(conn.get("user") or "hdfs")
    style = str(conn.get("addressing_style") or "path")
    conn["addressing_style"] = style if style in ("path", "virtual") else "path"
    conn["access_key"] = str(conn.get("access_key") or "")
    conn["secret_key"] = str(conn.get("secret_key") or "")
    conn["verify_ssl"] = bool(conn.get("verify_ssl", True))
    conn["use_proxy"] = bool(conn.get("use_proxy", False))
    return conn


def validate_connection(conn: dict[str, Any]) -> list[str]:
    """返回缺失的必填项标签，空列表表示通过。"""
    spec = CONNECTION_TYPES.get(conn.get("type"))
    if spec is None:
        return [f"未知的连接类型：{conn.get('type')}"]
    missing = []
    for field in spec["fields"]:
        if field.get("required") and not str(conn.get(field["key"]) or "").strip():
            missing.append(field["label"])
    if conn.get("endpoint") and not conn["endpoint"].startswith(("http://", "https://")):
        missing.append("Endpoint（需以 http:// 或 https:// 开头）")
    return missing


def find_connection(cfg: dict[str, Any], conn_id: str) -> dict[str, Any] | None:
    for conn in cfg.get("connections", []):
        if conn.get("id") == conn_id:
            return conn
    return None


def upsert_connection(data: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    conn = normalize_connection(data)
    conns = cfg["connections"]
    for index, existing in enumerate(conns):
        if existing.get("id") == conn["id"]:
            conns[index] = conn
            break
    else:
        conns.append(conn)
    save_config(cfg)
    return conn


def delete_connection(conn_id: str) -> bool:
    cfg = load_config()
    conns = cfg.get("connections", [])
    remain = [c for c in conns if c.get("id") != conn_id]
    if len(remain) == len(conns):
        return False
    cfg["connections"] = remain
    save_config(cfg)
    return True


def export_config() -> tuple[str, str]:
    """导出备份，返回 (建议文件名, JSON 文本)。"""
    cfg = load_config()
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"objstore-config-{stamp}.json"
    return filename, json.dumps(cfg, ensure_ascii=False, indent=2)


def import_config(text: str, merge: bool = True) -> dict[str, Any]:
    """导入备份。merge=True 时按连接 id 合并（收藏夹取并集），否则整体覆盖。"""
    incoming = json.loads(text)
    if not isinstance(incoming, dict):
        raise ValueError("备份内容不是合法配置对象")

    current = load_config()
    if not merge:
        incoming["settings"] = {
            **DEFAULT_SETTINGS,
            **(incoming.get("settings") if isinstance(incoming.get("settings"), dict) else {}),
        }
        incoming["connections"] = [
            normalize_connection(c) for c in incoming.get("connections", []) if isinstance(c, dict)
        ]
        incoming["favorites"] = normalize_favorites(incoming.get("favorites"))
        save_config(incoming)
        return load_config()

    by_id = {c["id"]: c for c in current.get("connections", [])}
    added = updated = 0
    for raw in incoming.get("connections", []):
        if not isinstance(raw, dict):
            continue
        conn = normalize_connection(raw)
        if conn["id"] in by_id:
            by_id[conn["id"]] = conn
            updated += 1
        else:
            by_id[conn["id"]] = conn
            added += 1
    current["connections"] = list(by_id.values())

    if isinstance(incoming.get("settings"), dict):
        current["settings"] = {**current["settings"], **incoming["settings"]}

    # 收藏夹按 path 去重取并集：导入的那份优先（名字以备份里的为准），已有条目顺序跟在后面
    mine = normalize_favorites(current.get("favorites"))
    theirs = normalize_favorites(incoming.get("favorites"))
    merged_remote = dict(mine["remote"])
    for conn_id, items in theirs["remote"].items():
        merged_remote[conn_id] = _clean_fav_items(items + merged_remote.get(conn_id, []))
    current["favorites"] = {
        "remote": merged_remote,
        "local": _clean_fav_items(theirs["local"] + mine["local"]),
    }

    save_config(current)
    result = load_config()
    result["_import_stats"] = {"added": added, "updated": updated}
    return result


def mask_secret(conn: dict[str, Any]) -> dict[str, Any]:
    """前端展示用：凭据字段打码，编辑时前端可按需取原值。"""
    safe = dict(conn)
    for key in SECRET_FIELDS:
        value = str(safe.get(key) or "")
        safe[key] = "" if not value else ("*" * 6 if len(value) <= 8 else f"{value[:2]}{'*' * 6}{value[-2:]}")
    return safe
