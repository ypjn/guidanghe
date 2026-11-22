# 归档盒桌面产品进度

## 基线

- 日期：2026-11-21
- 机器：macOS 27.2.1，Apple 芯片（arm64），数据卷可用约 112 GiB
- 源码目录：本仓库。目录名仍是历史的 stage5，内容已包含阶段五和阶段六
- 开工分支：`stage6/personal-file-agent`
- 开工提交：`cfaa5efe5e6c3f0e70b0eba3ab0c85a48a00112f`
- 工作区：干净，没有未提交改动
- 本地 `origin/main`：`da08bd7306fe71957eaa5d03b19db4a6e907e1f6`（阶段四）
- 远端：`git fetch` 失败（HTTP/2 framing 错误，随后 CONNECT 502）。没有把本地 main 合并进来，开发从阶段六提交继续
- 本仓库没有 `.codegraph`，没有自行建索引
- 本仓库没有 `.venv`。测试和构建解释器使用同级已验证环境：`/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python`（Python 3.14.8，PySide6 6.12.0，PyInstaller 6.22.3）。产品脚本只通过 `ARCHIVEBOX_BUILD_PYTHON` 接收该路径

## 开发分支

`product/desktop-platform`，从上述阶段六提交拉出。

## 01 Mac 应用与 DMG

任务：用户日志目录、统一 V1.8 身份、可选解释器的构建入口、图标、arm64 `.app` 和只读 DMG。

修复前检查（进程退出码 1）：

```sh
QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover -s tests -p 'test_product_packaging.py' -v
```

当时失败 3、错误 2：日志路径落在 `.app` 内；冻结运行仍写源码目录；没有 `app_metadata`；构建脚本写死 `.venv/bin/pyinstaller`。旧方案、偏好、最近空间和历史撤销那一项当时已经通过。

修复后：

- 同一 unittest：7 项通过，进程退出码 0
- `tests/test_p2_fixes_20261113.py`：PASS 24 / FAIL 0 / SKIP 0，进程退出码 0。没有改写仓库里原有的 Windows 结果文件
- 源码解释器上的 `--product-acceptance`：imports、organize、reopen-undo、learning-agent、upgrade-seed、upgrade-check、smoke-gui 的进程退出码都是 0

构建和安装镜像验收在本批代码提交之后进行，身份文件记录的是该提交。结果补记在下面。

### 构建与安装验收

代码提交：`1a7d5f1e9123e13ff792a8af9485f71ee6582278`。构建命令：

```sh
ARCHIVEBOX_BUILD_PYTHON="/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码/.venv/bin/python" zsh 打包/build_mac.sh
```

- 应用：`打包/dist/归档盒.app`，Mach-O arm64，`CFBundleShortVersionString` 1.8.0，`ArchiveBoxBuildCommit` 为上述提交
- DMG：`打包/dist/归档盒-1.8.0-macos-arm64.dmg`，147 MiB，UDZO 压缩只读
- SHA-256：`afef9a1990643f03a4c3bf9a973c11a2f83037c0f23d548ae5613dffb7d5514d`
- 本地 ad-hoc 签名。未做 Apple 开发者签名和公证

安装验收命令：

```sh
zsh 打包/accept_mac_app.sh 打包/dist/归档盒-1.8.0-macos-arm64.dmg docs/product-development/evidence/batch01
```

进程退出码（不是 `open` 的退出码）：imports、organize、reopen-undo、learning-agent、upgrade-seed、upgrade-check、smoke-gui 都是 0。脚本总退出码 0。镜像只读挂载后复制到临时“应用程序”目录，卸载后继续运行。运行前后应用包指纹都是 1309 条且一致。启动日志写在验收工作目录或 `~/Library/Logs/归档盒/启动日志.txt`，包内没有新日志。

原生窗口：`show-and-quit` 的应用进程退出码 0。全屏 `screencapture` 失败，原因是 `could not create image from display`。随后直接打开 `打包/dist/归档盒.app`，窗口标题为「归档盒 V1.8」，能看到学习建议和图片文字识别入口。截图在 `docs/product-development/evidence/batch01/native-window.webp`。没有用辅助功能逐项点击扫描、确认和撤销；这些动作由打包后的可执行文件在验收命令里完成。窗口关闭后没有残留 `archivebox` 进程。

已知未测：

- Apple 开发者签名和公证
- 辅助功能权限下的手工点击
- Windows 原生安装

## 02–07

未开始。
