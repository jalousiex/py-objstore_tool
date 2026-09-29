"""程序入口：起本地服务 → 自动打开浏览器。

只监听 127.0.0.1；关闭控制台窗口或界面点「退出服务」即停止。
"""

from __future__ import annotations

import sys
import threading
import webbrowser

from . import __version__, config as cfg
from .server import DEFAULT_PORT, create_server

BANNER = """
  objstore_tool  v{version}
  ------------------------------------------------------------
  配置文件 : {config_file}
  服务地址 : {url}
  ------------------------------------------------------------
  浏览器会自动打开；关掉本窗口即停止服务。
"""


def _enable_utf8_console() -> None:
    """Windows 控制台默认 GBK，直接输出中文可能抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _enable_utf8_console()

    conf = cfg.load_config()
    settings = conf.get("settings") or {}
    try:
        preferred = int(settings.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        preferred = DEFAULT_PORT

    server = create_server(preferred_port=preferred)
    port = int(server.server_address[1])
    url = f"http://127.0.0.1:{port}/"

    print(BANNER.format(version=__version__, config_file=cfg.config_file(), url=url))

    if port != preferred:
        print(f"  提示   : 端口 {preferred} 已被占用，本次改用 {port}（改端口请编辑配置文件）")

    if settings.get("open_browser", True):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止服务...")
    finally:
        server.server_close()

    print("服务已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
