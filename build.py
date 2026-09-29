"""打包脚本（可选路径，日常用不着）。

    uv run --with pyinstaller python build.py            # 目录版（默认），启动快
    uv run --with pyinstaller python build.py --onefile  # 单个 exe，便于拷贝

PyInstaller 用 ``--with`` 按需注入，不进项目环境。

boto3 + botocore 体积不小：目录版约 60MB（数百个文件），单文件 exe 约 30–50MB，
且单文件每次启动都要解压到 %TEMP%，比直接跑脚本慢。
本机自用请优先 ``uv tool install .`` —— 只有 uv 的一个隔离环境，启动无解压开销。
只有在目标机器没有 Python、又不方便装 uv 时才需要打包。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_SEP = ";" if sys.platform == "win32" else ":"

# 运行时动态导入的模块，PyInstaller 静态分析看不到，必须显式声明
HIDDEN_IMPORTS = [
    "objstore_tool.adapters.s3",
    "objstore_tool.adapters.webhdfs",
]

# 用不到的大件，显式排掉以压体积
EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "numpy",
    "pandas",
    "PIL",
    "matplotlib",
    "boto3.examples",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把 objstore_tool 打包成可执行文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--onedir", action="store_true", help="打成目录（默认，启动快）")
    mode.add_argument("--onefile", action="store_true", help="打成单个 exe（拷贝方便，启动慢）")
    parser.add_argument("--clean", action="store_true", help="打包前清理 build/ dist/ 与 spec")
    return parser.parse_args()


def entry_name() -> str:
    return "objstore_tool.exe" if sys.platform == "win32" else "objstore_tool"


def main() -> int:
    args = parse_args()
    mode = "onefile" if args.onefile else "onedir"

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller。请用：uv run --with pyinstaller python build.py")
        return 1

    if args.clean:
        for name in ("build", "dist", "objstore_tool.spec"):
            target = ROOT / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        f"--{mode}",
        "--name", "objstore_tool",
        "--console",
        "--add-data", f"{ROOT / 'objstore_tool' / 'web'}{DATA_SEP}web",
    ]
    for item in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", item]
    for item in EXCLUDES:
        cmd += ["--exclude-module", item]
    # run.py 是包外的顶层入口，不能省：objstore_tool 包内用的是相对导入，
    # PyInstaller 没法直接把 objstore_tool/main.py 或 __main__.py 当分析入口。
    # 它只在这条打包路径上出现，日常运行（start.bat / objstore_tool 命令）不经过它。
    cmd.append(str(ROOT / "run.py"))

    print("执行：", " ".join(cmd))
    print()
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\n打包失败，请查看上方 PyInstaller 输出。")
        return result.returncode

    dist = ROOT / "dist"
    artifact = dist / entry_name() if mode == "onefile" else dist / "objstore_tool" / entry_name()
    print()
    print(f"打包完成：{artifact}")
    print()
    print("使用提示：")
    print("  · 首次运行会在程序所在目录创建 .config/，拷走程序 + .config/ 即完成迁移")
    print("  · 打包只是分发手段；本机自用建议 uv tool install . ，无需解压、启动更快")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
