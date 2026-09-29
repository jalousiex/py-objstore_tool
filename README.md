# objstore_tool

本地对象存储管理工具，通过浏览器管理 S3 兼容存储（MinIO、阿里云 OSS、腾讯云 COS、AWS S3）和 WebHDFS。无需 IDE 或外部服务。

## 快速开始

需要 `uv` 和 Python 3.11。本机没有 Python 3.11 时先运行 `uv python install 3.11`。

在项目目录运行：

```bash
uv run objstore_tool
```

也可以双击项目根目录的 `start-web.bat`，或在 VS Code 中运行默认任务「运行 objstore_tool」。启动后浏览器会自动打开。服务仅监听本机 `127.0.0.1`，关闭控制台窗口或点击「退出服务」即可停止。

## 安装到 PATH

如需在任意目录运行 `objstore_tool`：

```bash
uv tool install --python 3.11 .
objstore_tool
```

更新安装：

```bash
uv tool install --force --python 3.11 .
```

## 支持的存储与功能

- S3 兼容存储及 WebHDFS；连接可在界面中添加并测试
- 桶和目录浏览、本地目录双栏查看、收藏夹
- 上传、下载、新建目录、递归删除，以及文件和目录复制 / 移动
- S3 支持预签名下载链接；文本和图片可直接预览

复制和移动支持同一连接内跨桶操作，不支持跨连接。工具不支持删除桶或重命名。

## 配置与备份

配置文件为 `.config/connections.json`，路径取决于运行方式：

| 运行方式 | 配置目录 |
|---|---|
| 项目源码运行 | 项目根目录 `.config/` |
| `uv tool install` | `%LOCALAPPDATA%\objstore_tool\.config\` |
| 打包的 exe | exe 所在目录 `.config/` |

备份该文件或整个 `.config/` 目录即可。连接凭据以明文保存在本机配置中，请妥善保管；界面也提供「导出备份 / 导入备份」。收藏夹同样保存在配置文件中。可通过环境变量 `OBJSTORE_CONFIG_DIR` 指定配置目录。

## 自测

两个脚本都会启动本地 mock 服务，不需要连接真实存储。为避免改动日常配置，先将配置目录指向临时位置：

```powershell
$env:OBJSTORE_CONFIG_DIR = "$env:TEMP\objstore-selftest"
uv run python tests/selftest.py
uv run python tests/selftest_webhdfs.py
```

## 打包（可选）

```bash
uv run --with pyinstaller python build.py
uv run --with pyinstaller python build.py --onefile
```

默认生成目录版 `dist/objstore_tool/objstore_tool.exe`；添加 `--onefile` 生成单文件 `dist/objstore_tool.exe`。将程序与旁边的 `.config/` 目录一同携带即可。
