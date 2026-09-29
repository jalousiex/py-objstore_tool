"""通用入口脚本。

两个用途：
- 直接运行：``python run.py``（等价于 ``uv run objstore_tool``）
- 打包 exe 时作为 PyInstaller 的入口点 —— 包内模块用的是相对导入，
  需要一个顶层脚本来启动。
"""

from objstore_tool.main import main

if __name__ == "__main__":
    raise SystemExit(main())
