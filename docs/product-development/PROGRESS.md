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

## 02 多空间本地索引

完成。索引库在用户数据目录 `index/index.db`，说明见 `docs/product-development/index-storage.md`。

```sh
QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover -s tests -p 'test_product_index.py' -v
```

6 项通过，进程退出码 0，用时 60.7 秒。1 万个小文件冷索引 3.14 秒、热刷新 3.36 秒、重读 0。随后加到 5 万，增量 24.77 秒，热刷新重读仍是 0。源目录清单和 `.organizer/history.db` 在索引后保持不变。

macOS 在 size、mtime_ns、ctime_ns 都一致时复用内容校验。Windows 的 ctime 不是内容变更时间，刷新时重读，不把 stat 当成内容未变的证明。

## 03 PDF 与中文正文检索

完成。依赖 `pypdf==6.4.0`（BSD-3-Clause），已写入 `归档盒源码/requirements.txt`，并加入 Mac 打包的隐藏导入。当前已安装的 DMG 还是第 01 批的构建，不含这批代码。

```sh
QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover -s tests -p 'test_product_content.py' -v
```

6 项通过，进程退出码 0，用时 0.14 秒。60 份混合样本建正文索引 0.03 秒，库文件 212992 字节。30 条事先写好的查询全部命中应有文件，排除列表里的文件没有出现，30 次查询合计 0.002 秒。

样本里的 PDF 用标准字体写入可复制文字，所以页内文字是 ASCII；中文查询落在 txt、docx、pptx 和 xlsx 上。同名 `资料.pdf` / `资料.docx` 靠正文区分。PDF 第 3 页、幻灯片 2、工作表 2，以及超过 4KB 之后的正文和代码都能查到，位置写在结果里。加密 PDF 的原因是「PDF 已加密，未读取正文」，损坏 PDF 的原因是「PDF 无法解析」，空白 PDF 是「没有可提取文字，未做扫描识别」。这三份没有中断后面的文件。

一到两个汉字走连续匹配，三个字及以上走 FTS5 trigram。引号、减号、通配符、SQL 样式和超长输入没有抛出未处理异常，也没有改动文件行数。读取中途改文件、把文件换成空间外链接、取消后立即再更新、重建失败后再查询、从版本 1 的索引升级，都按源文件状态保留或丢弃正文。全文读取关闭时，原有规则仍能移动并撤销，校验值不变。

第 02 批的索引测试在接上正文提取后重新跑过。不含 1 万文件的 5 项进程退出码 0。1 万个小文本文件冷索引 8.95 秒、热刷新 1.40 秒、重读 0；加到 5 万后增量 171.80 秒，热刷新重读仍是 0。变慢是因为每个新文本都会写入正文片段，不是因为重读了未变化的文件。进程退出码 0。

## 04 统一搜索

完成查找闭环。主窗口有「搜索」。查找不生成移动计划；只有明确要整理、归入或移动时，才对当前空间里选中的在线文件做计划，确认后才移动。受保护项目可以搜到，但不会进入移动计划。同名文件按空间和相对路径定位。查询有代次，旧结果不会盖住新结果。尚未做语义检索，界面上写明了这一点。原有课程管家没有改成默认移动。

```sh
QT_QPA_PLATFORM=offscreen "$ARCHIVEBOX_PYTHON" -m unittest discover -s tests -p 'test_product_search.py' -v
```

4 项通过，进程退出码 0，用时 0.42 秒。20 条事先写好的句子，动作、关键词、排除词、类型，以及修改时间或创建时间的绝对日期都符合预期。「最终版/最新版」不会按修改时间认定定稿。否定条件和上周修改时间能排除不该出现的文件；把修改时间改到上周并不会让「上周创建」命中。查找前后文件内容不变。高数作业经确认后移动并撤销，校验值还原。

离屏窗口截图在 `docs/product-development/evidence/batch04/`，宽度 820、1024、1280。长中文路径会换行，搜索、定位、整理和确认按钮都在窗口内。没有在原生窗口里逐项点击。

## 05 外置盘只读索引与离线目录

完成（隔离夹具；真盘拔插待用户环境验收）。

- `spaces` 表扩展：`volume_id`、`filesystem`、`online`、`last_indexed_at`；`SCHEMA_VERSION` 3，自版本 2 事务迁移并回填在线卷身份。
- `volume_identity.py`：Linux/macOS/Windows 下优先 UUID 或卷序列号，辅以 `st_dev`；盘符或卷名单独不能证明同一盘。
- 登记/刷新核对卷身份；离线保留目录与正文；查找标注离线及索引时间；定位拒绝陈旧路径与错盘同名文件。
- 取消、I/O、权限失败、断连不全量标删；移除登记只清理本地索引与正文，不动来源与 `.organizer`。

```sh
QT_QPA_PLATFORM=offscreen /agent/repos/guidanghe/.venv/bin/python -m unittest discover \
  -s 归档盒源码/tests -p 'test_product_volume.py' -v
```

5 项通过（离线保留、同名异盘、不完整扫描、本地移除、schema 2→3 迁移）。另有 `test_product_index.py` 中根目录缺失覆盖为 `offline`。

未测：Windows NTFS/exFAT 真盘、用户安全弹出与重连 UI、junction 逃逸实测。

## 06 本地版本备份与安全恢复

完成。新增 `归档盒源码/backup_store.py`、`归档盒源码/backup_service.py`；索引/整理默认跳过 `.archivebox-backup`。

```sh
QT_QPA_PLATFORM=offscreen /agent/repos/guidanghe/.venv/bin/python -m unittest discover \
  -s 归档盒源码/tests -p 'test_product_backup.py' -v
```

11 项通过，进程退出码 0。夹具：备份 v1 → 改样本 → 备份 v2 → 分别恢复到新空目录，独立清单路径/数量/SHA-256 一致；改恢复副本不影响来源与其它版本。覆盖取消、未完成清单、对象篡改/缺失、恶意清单路径、非空目标拒绝、仓库嵌套、整理保护显式授权备份。

元数据见 `metadata_support_matrix()`：仅内容与可记录修改时间等，不声称扩展属性或整盘备份。

## 07 集成构建与发布验收

完成可重复的发布准备与功能验收（本机为 Linux，正式 `.dmg` 仍须在 Apple 芯片 Mac 上生成）。

- 版本升至 **1.8.1**；`打包/archivebox.spec` 纳入 `volume_identity`、`backup_*`。
- `打包/requirements-windows.txt` 补上 `pypdf==6.4.0`；新增原生 `打包/build_windows.ps1`。
- `打包/make_release_bundle.sh` 打出可交给 Mac 一键生成 DMG 的源码发布包。
- `打包/verify_release_features.py`：隔离目录核对搜索反例、备份恢复、schema 升级、新模块导入。

```sh
bash 打包/make_release_bundle.sh
.venv/bin/python 打包/verify_release_features.py
```

Mac 正式 DMG：

```sh
export ARCHIVEBOX_BUILD_PYTHON=...
zsh 打包/build_mac.sh
zsh 打包/accept_mac_app.sh 打包/dist/归档盒-1.8.1-macos-arm64.dmg docs/product-development/evidence/batch07
```

未测：本环境无 `hdiutil`/`codesign`，不能在此生成或签名真实 DMG；Windows 真盘与高 DPI 原生窗口待 Windows 机验收。

## A 备份桌面入口（Windows，2026-11-22）

机器：Windows 11 家庭版，10.0.26200，Python 3.14.7 + 仓库 .venv（PySide6 6.12.0）。
分支：`product/desktop-finish-win`（自 `handoff/desktop-finish-20261122` @ `b820189`）。

实现：
- 主窗口侧栏新增「版本备份」→ `backup_dialog.py` / `backup_worker.py`
- `preview_backup` / `verify_version` 增加可选 `cancel_event` / `progress_cb`；哈希与大文件分块复制可中途取消；提交完整清单前再核对取消
- 版本备注 `note` 写入并回读 `manifest.json`
- Windows 下 `validate_restore_rel_path('/abs')` 正确拒绝 POSIX 绝对路径
- 活跃 worker 接入 `_background_workers` / `_close_child_windows`；关对话框等待线程结束

验收命令（源码目录，进程退出码 0）：

```
cd 归档盒源码
$env:QT_QPA_PLATFORM='offscreen'
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_product_backup*.py' -v
```

20 项通过（既有 backup 11 + UI/生命周期 9），EXIT=0。
离屏截图：`docs/product-development/evidence/batchA-win/`（820/1024/1280、预览、版本列表、错误态）。
原生窗口逐项点击与真盘验收仍待本机后续批次；Linux 离屏截图不记作 Windows 原生点击。

## B Windows 卷身份与索引（2026-11-22）

机器：Windows 11 10.0.26200；已连接外置盘 G: 标签 YPJN，文件系统 exFAT，卷身份 win:6A12D786。
本机 D: NTFS win:420AA01A；系统盘 C: NTFS win:68C42C93。

实现：
- 修复子路径卷标探测（GetVolumeInformationW 改用盘符根）
- 索引扫描跳过 Windows junction/重解析点，防止越界登记
- 新增 reassociate_space；文件空间对话框显示卷身份/当前探测，支持重新关联
- 增加 Windows 序列号与 junction 回归；真盘脚本 打包/windows_volume_realdisk_check.py

夹具验收：

```
cd 归档盒源码
$env:QT_QPA_PLATFORM='offscreen'
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_product_volume.py' -v
```

8 项通过，EXIT=0。

真盘只读登记→弹出→离线→重连：等待用户指定「资料只读目录」与「可写测试目录」后再跑
打包/windows_volume_realdisk_check.py；未指定前不写 G: 原有资料。


## C Windows 独立成品（2026-11-22）

修复并验收：
- 统一包布局为 `runtime\pythonw.exe` + `app\` + `归档盒.exe`（与 Go 启动器一致）
- 嵌入式 Python 用构建解释器 `pip install --target`（embed 无 pip）；配置 `python*._pth`（import site / Lib\\site-packages / ..\\app）
- 原生命令失败即停；无 commit 不打正式包；产品文件白名单；启动器透传参数、日志写用户目录
- 免安装包日志不再写安装目录，避免改指纹
- 新增 `打包/accept_windows_app.ps1`、`requirements-windows-portable.txt`（核心 PySide6+pypdf；OCR 可选）

构建（本机）：

```
powershell -File 打包/build_windows.ps1 -PythonExe .venv\Scripts\python.exe -GoExe 打包\_tools\go\bin\go.exe -EmbedPythonZip 打包\_tools\python-3.14.0-embed-amd64.zip
```

产物（构建时提交写入 identity；最终以推送前提交为准）：
- ZIP：`打包/dist/归档盒-1.8.1-windows-x64.zip`
- 包内 `smoke-gui` 经 `归档盒.exe` 退出码 0；包内无 `启动日志.txt`
- 证据：`docs/product-development/evidence/batchC-win/`

未测：干净新用户账户完整手工点击；OCR 捆绑包；代码签名。


## E Windows 独立复核（2026-11-22）

见 `docs/product-development/ACCEPTANCE.md`。固定提交 `af1a5d2`；ZIP SHA-256 `95eee7d2c5db1e649ddea46027fe9987c985a3c6a0b2efde6952491f2f737305`；`归档盒.exe` smoke-gui 退出码 0。真盘与 Mac 仍待测。

