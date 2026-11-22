# 归档盒

本地桌面文件整理工具，当前版本 **V1.8.1（1.8.1）**。用 Python、PySide6 和 SQLite。默认离线：不调用付费接口，不上传文件正文，也不做云盘。

选一个文件夹后，可以预览整理方案，确认后才移动文件。每次移动都有历史，可以撤销。模型和建议只生成方案，文件变更仍然走预览、确认、执行和撤销。

## 下载（成品包）

产品页：[https://guidanghe.apps.grok.bot/](https://guidanghe.apps.grok.bot/)（源码在仓库 `website/`）。Mac 与 Windows **不是同一套安装包**。

| 平台 | 形态 | 获取 |
| --- | --- | --- |
| **Windows x64** | 免安装 ZIP（解压后双击 `归档盒.exe`） | [直接下载](https://github.com/ypjn/guidanghe/releases/download/v1.8.1-windows/-1.8.1-windows-x64.zip) · [Release 页](https://github.com/ypjn/guidanghe/releases/tag/v1.8.1-windows) |
| **OCR 选装** | RapidOCR 完整包（不进标准 ZIP） | 制作中，上线后挂产品页 |
| **macOS Apple 芯片** | `.app` + 只读 DMG | 需在 Apple 芯片 Mac 上按 [打包/构建说明.md](打包/构建说明.md) 生成；正式 Release 尚未挂 DMG |

**Windows 包校验（SHA-256）**

```
95eee7d2c5db1e649ddea46027fe9987c985a3c6a0b2efde6952491f2f737305
```

对应 Release 资源名：`-1.8.1-windows-x64.zip`（约 256&nbsp;MB；构建提交 `af1a5d2`）。未做代码签名，SmartScreen 可能提示「未知发布者」——请对照上面的哈希核验后再使用。

> 含「AI 整理顾问 → 可勾选预览」的最新源码在分支 `product/desktop-finish-win`。当前 Windows Release ZIP 仍是顾问功能合入前的构建；要用最新 AI 入口，请拉取该分支后按构建说明重新打 ZIP，或等下一版 Release。
>
> **不做本地大模型**；AI 仅自备 API。文件夹监视若以后做，默认关闭、须手动打开。

## 现在能做什么

- 按课程、类型或学期规则整理文件，并撤销到原来的位置
- 从改单记录里提出学习建议；确认后才写成偏好
- **AI 整理顾问**：先扫目录结构，再多轮对话调整；可生成主界面可勾选预览（需自备 API Key）
- 用一句话本地查找课程材料（AI 管家）；整理和查找分开
- 登记多个文件夹，做本地索引和增量更新
- 在已登记的空间里按文件名和正文搜索，包括 PDF 里可复制的文字和中文
- 受保护的项目可以只读纳入索引，但不会因此被移动
- 本地版本备份与恢复到新空目录

扫描版 PDF 不渲染、也不做文字识别。语义检索还没有做，搜索窗口里写了这一点。程序不内置任何 AI 密钥。

## Mac / Windows 两套方案（对照）

| | macOS（arm64） | Windows（x64） |
| --- | --- | --- |
| 构建 | `打包/build_mac.sh` → `.app` + DMG | `打包/build_windows.ps1` → 免安装 ZIP |
| 验收 | `打包/accept_mac_app.sh`（功能链较全） | `打包/accept_windows_app.ps1`（目前以 imports + smoke-gui 为主） |
| 运行时 | PyInstaller 冻结应用 | 嵌入式 Python + Go 启动器 `归档盒.exe` |
| 签名 | 本地 ad-hoc；开发者签名/公证未做 | 未签名 |
| OCR | 可走系统 Vision 等 | 便携核心包默认不捆绑 OCR |
| 用户数据 | `~/Library/Application Support/归档盒`、`~/Library/Logs/归档盒` | `%APPDATA%\归档盒`、`%LOCALAPPDATA%\归档盒` |

源码与业务逻辑同一套；**分发物、依赖裁剪和系统对接分开维护**。细节见 [打包/构建说明.md](打包/构建说明.md)。

## 运行（源码）

需要 Python 3.14，以及 `归档盒源码/requirements.txt` 里的依赖。在 macOS 上还需要系统自带的 Vision（通过 `pyobjc`）。仓库里可以没有 `.venv`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r 归档盒源码/requirements.txt
cd 归档盒源码
../.venv/bin/python main.py
```

Windows PowerShell 示例：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r 归档盒源码\requirements.txt
cd 归档盒源码
..\.venv\Scripts\python.exe main.py
```

界面是中文的。先选择要整理的文件夹，再生成预览。要用联网 AI，请打开「设置 → 配置 AI 接口」粘贴自己的 Key 或厂商脚本。

## 数据放在哪里

| 内容 | macOS | Windows |
| --- | --- | --- |
| 方案库、全局偏好、AI 设置、顾问会话 | `~/Library/Application Support/归档盒` | `%APPDATA%\归档盒` |
| 启动日志 | `~/Library/Logs/归档盒` | `%LOCALAPPDATA%\归档盒` |
| 某个文件夹的整理历史、偏好、撤销 | 该文件夹下的 `.organizer` | 同左 |
| 多空间索引和正文片段 | 应用数据目录里的 `index/index.db` | 同左 |

这些都不写进 `.app`、DMG、ZIP 或源码包。索引可以重建。整理历史、偏好和撤销记录在升级时保留。

测试时可以用 `ARCHIVEBOX_APP_DIR`、`ARCHIVEBOX_LOG_DIR`、`ARCHIVEBOX_INDEX_DB` 把上述路径指到临时目录。

## 测试

在 `归档盒源码` 目录下执行。界面测试使用离屏 Qt。

```sh
QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m unittest discover -s tests -p 'test_product_*.py' -v
```

AI 顾问全流程（假传输 + 截图证据）：

```sh
QT_QPA_PLATFORM=offscreen ../.venv/bin/python tests/desktop_check_ai_consult_flow.py
```

更早的阶段测试仍在 `tests/` 里，文件名带阶段或日期。

## 打包

- **macOS（Apple 芯片）**：`打包/build_mac.sh` 生成 `.app` 和只读 DMG；解释器通过 `ARCHIVEBOX_BUILD_PYTHON` 传入。
- **Windows x64**：`打包/build_windows.ps1` 生成免安装 ZIP。

步骤、依赖和验收命令见 [打包/构建说明.md](打包/构建说明.md)。请使用本仓库最新提交重新构建；旧安装包可能不含搜索修复、备份、卷身份或 AI 顾问。Apple 开发者签名/公证与 Windows 代码签名都还没做。

## 目录

- `归档盒源码/`：程序和测试
- `打包/`：Mac / Windows 构建与验收脚本
- `docs/product-development/`：桌面产品进度、验收与证据（含 AI 顾问截图）
- `课程交付/`、`归档盒-项目设计/`：课程材料和历史设计，产品开发不改这些记录

## 还没做

Apple 开发者签名/公证，Windows 代码签名，Windows 真盘拔插与原生窗口完整点击验收，以及把最新 AI 顾问打进下一版 Windows/Mac Release。同步、NAS 和手机不在这一版里。
