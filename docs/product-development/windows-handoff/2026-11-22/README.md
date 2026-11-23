# Windows 原生开发与外置盘测试交接

日期：2026-11-22。用户已授权将后续开发转到 Windows Cursor，利用连接在 Windows 上的真实外置盘完成开发和原生验证。

仓库：<https://github.com/ypjn/guidanghe>

交接来源分支：`product/desktop-platform`

交接前代码基线：`7e77469114676450fe00ee59c9b128ac6d7d8e4b`。

本目录与产品源码一起提交，不依赖 Mac 绝对路径、原聊天或本地任务包。只上传开发交接和可重建夹具，不包含用户资料、密钥、虚拟环境或安装包。

## 在 Windows 怎么开始

让 Cursor 读取本目录的 [CURSOR_WINDOWS_PROMPT.txt](CURSOR_WINDOWS_PROMPT.txt)。它需要：

1. 克隆或更新本仓库，保留已有未提交改动，以最新 `origin/product/desktop-platform` 为基础。
2. 新建或继续合适的 `product/windows-desktop` 开发分支，不直接改 main，不强推。
3. 读取 [任务与验收](TASKS.md)、[搜索复核](SEARCH_REVIEW.md)、当前项目 [PROGRESS.md](../../PROGRESS.md)。
4. 核对 Windows 原生环境，先复现并修复搜索，再做外置盘、备份和 Windows 新包。

原生 Windows 指 Windows Python、Win32 文件语义和桌面窗口。WSL 不替代这部分验收。不要求 GPU 或 CUDA。

## 当前已有什么

- 第01批：阶段六 Mac `.app` 与 DMG、用户日志目录、图标、V1.8身份和安装验收。
- 第02–04批：多空间索引、PDF/中文正文检索、统一搜索与查找/整理分离。
- 现有 Mac 安装包的构建提交仍为 `1a7d5f1e9123e13ff792a8af9485f71ee6582278`，不含第02–04批。
- 第05外置盘、第06备份、第07最新集成包尚未实施。
- 2026-11-22在Mac重跑搜索4项、内容6项通过；独立样本又发现3个分页/筛选缺陷。旧测试通过不等于本轮已验收。

## 本轮顺序

**搜索修复 → Windows真实外置盘只读索引 → 本地版本备份恢复 → Windows集成包与原生验收 → 推送交接给Mac。**

本轮不做语义检索、同步、NAS和手机端。共享代码保持Mac兼容，Mac原生回归与DMG由Mac后续完成；不在Windows上伪称已验证Mac应用。

## 文件与证据

- `CURSOR_WINDOWS_PROMPT.txt`：完整执行指令。
- `TASKS.md`：环境、实现边界、分批验收、打包和回传要求。
- `SEARCH_REVIEW.md`：三个已复现问题和正常预期。
- `reproduce_search.py`：便携的临时夹具探针，自动从当前仓库找到源码；不修改产品代码和真实资料。
- `mac-baseline-results.json`：Mac基线复跑结果，绝对本机路径已隐去，断言和数量未改。

真实外置盘的盘符、文件系统和允许测试的目录必须在Windows端确认，不能把Mac环境快照当成Windows实测。未获得测试目录时，先继续隔离夹具中的实现、回归和打包准备。
