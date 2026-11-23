# 归档盒桌面交付收尾 Implementation Plan

> **For agentic workers:** execute this plan task-by-task, in order. Steps use checkbox (`- [ ]`) syntax for tracking. Track tasks with your plan/todo tracking tool; do not start a task until the previous task's verification step passed.

**Goal:** 将已有1.8.1核心代码接入可操作桌面流程，并完成Windows真盘/独立包、MacDMG、独立复核和准确发布材料。

**Architecture:** 复用现有backup_service/backup_store、schema3索引、搜索和文件执行层。新增独立备份对话框与worker，小幅补全服务取消/进度；两端分别构建与验证，发布材料消费固定候选与真实产物信息。

**Tech Stack:** Python、PySide6、SQLite、现有嵌入式Python/Go启动器、PowerShell、PyInstaller和macOS磁盘镜像工具。

**Spec:** 本目录00、A–F完整提示词；当前`docs/product-development/PROGRESS.md`和已有交付记录作为实现线索，不替代本轮验证。

## Global Constraints

- 基线`6ccd97a678afa8367b55123d6ddd8c0064722bd8`；执行时保留远端/本地后续更新和未提交改动。
- 不直接合并main、不强推、不回退到旧V1.7代码；旧Windows阶段证据不替代本轮验收。
- 真实资料只读；写入、删除、恢复、故障注入只用临时夹具或用户指定的测试样本目录。
- 查找不移动；整理预览确认、保护、执行校验、历史撤销保持现有边界。
- 默认离线；不上传真实资料或密钥，不启用额外付费接口。
- Windows真盘/桌面、Mac原生/DMG、Linux夹具分别记录；缺设备不伪造通过，独立任务可继续。
- 测试命令中的`python`指执行者已核对的实际项目解释器，不假定裸PATH或另一个环境有包就可用。

## Review Focus

1. 预览/验证/哈希长任务在UI中假取消、退出时仍有线程；由Task A固定调度和大文件取消测试验证。
2. 旧预览/旧版本或授权变化后仍执行；由Task A代次与确认失效断言验证。
3. 同名盘/盘符复用、部分扫描使旧索引误关联或清空；由Task B真实设备与身份注入测试验证。
4. Windows包依赖开发环境、目录不匹配或原生命令失败仍压缩；由Task C清洁环境与构建故障注入验证。
5. 恢复清单/临时路径/目录替换造成越界写或删未知文件；由Task E哨兵与并发替换夹具验证。

## Task A: 备份桌面入口和生命周期

**Files:** Create `归档盒源码/backup_dialog.py`、`归档盒源码/backup_worker.py`、`归档盒源码/tests/test_product_backup_ui.py`；Modify `归档盒源码/main.py`、`归档盒源码/backup_service.py`及确有必要的底层分块操作。

**Interfaces:** 复用`preview_backup`、`run_backup`、`list_backup_versions`、`verify_version`、`restore_version`；预览/验证新增可选`cancel_event=None, progress_cb=None`，保持旧调用兼容。全部活跃worker接入主窗口已有任务枚举与关闭链。

- [ ] 写UI实际调用服务、旧预览失效、非空恢复拒绝、取消/关窗等待的正常预期检查，先证实当前未满足。
- [ ] 实现独立对话框、后台预览/校验/复制、确认和版本浏览，小幅补全服务取消。
- [ ] 在源码目录运行`python -m unittest discover -s tests -p 'test_product_backup_ui.py' -v`与现有备份回归；全部通过且真实OS返回0。
- [ ] 实际窗口做V1/V2及分别恢复，源和仓库原对象不变，保存非空截图和独立清单；提交本批。

## Task B: Windows真盘和文件语义

**Files:** Modify必要的`归档盒源码/volume_identity.py`、`归档盒源码/index_service.py`、`归档盒源码/index_dialog.py`等；Extend `归档盒源码/tests/test_product_volume.py`；Create Windows真盘操作记录/夹具脚本。

**Interfaces:** 沿用schema3、空间ID和卷身份入口，不新建平行索引。盘身份不可核实则重新关联，离线仍保留历史只读查询。

- [ ] 先登记授权测试位置，写同名换盘/路径复用/部分扫描/越界链接断言，记录真实设备可覆盖范围。
- [ ] 修实际Windows兼容问题，运行`python -m unittest discover -s tests -p 'test_product_volume.py' -v`，全部通过且OS返回0。
- [ ] 原生窗口登记、搜索、用户安全弹出、离线查找、重连；对只读范围清单核对，真机和模拟分别记录并提交。

## Task C: Windows独立成品

**Files:** Modify `打包/build_windows.ps1`、`打包/win-launcher/main.go`、依赖/资源/版本入口；Create `打包/accept_windows_app.ps1`和有区分力的构建检查。

**Interfaces:** 选定一种包内运行时目录，exe/bat/脚本共用；参数传到Python，实际子进程退出可核对；产物身份对应固定提交。

- [ ] 在Windows证明当前目录/pip/退出码/依赖或源码夹带缺口，写构建失败不发布ZIP的检查。
- [ ] 修嵌入式部署、产品文件白名单、原生命令退出处理和启动链。
- [ ] 构建最终ZIP，从中文空格路径解压，在不依赖系统Python/源码的环境双击和运行新功能。
- [ ] 核对真实退出、包内不写入、非空旧数据升级和哈希；提交并进行Task E的Windows部分，回传Mac。

## Task D: Mac兼容与真实DMG

**Files:** Modify受共享改动影响的平台逻辑、`打包/archivebox.spec`、`打包/build_mac.sh`、`打包/accept_mac_app.sh`和`归档盒源码/product_acceptance.py`；新增原生证据。

**Interfaces:** 接收Windows固定提交，沿用`ARCHIVEBOX_BUILD_PYTHON`与`app_metadata`构建身份，输出真实.app/DMG。

- [ ] 核对接回提交/依赖/schema，跑受影响Mac回归和原生新功能流程。
- [ ] 打包并从DMG复制、卸载后独立运行；源码模拟验收不能代替此步骤。
- [ ] 检查两版恢复、旧数据兼容、包指纹、真实退出和哈希，完成Task E的Mac部分并提交。

## Task E: 独立复核和返修

**Files:** Create `docs/product-development/ACCEPTANCE.md`及必要针对性测试；修复仅涉及成立问题的模块。

**Interfaces:** 消费固定源码/产物身份与两端证据，输出问题→复现→回归→修复→复验的映射和支持矩阵。

- [ ] 固定候选，按E完整提示词检查文件安全、索引、迁移、临时路径、线程和真实入口。
- [ ] 成立问题先复现并写失败回归，再修根因；跑受影响用例，不放宽断言或机械重复。
- [ ] 有新修改才重建受影响产物，确认最终身份；阻断已解决再形成发布候选与未测矩阵。

## Task F: 真实发布材料与官网接续

**Files:** Create/Update `docs/product-development/RELEASE.md`、`TRYOUT.md`、`release-manifest.json`；网站工程确认后再编辑其真实文件。

**Interfaces:** 只消费已验证候选与真实产物URL/哈希；没有下载入口不伪造，没有网站工程不新建重复站点。

- [ ] 生成平台、版本、提交、产物、SHA-256、签名和限制的清单及10分钟试用说明。
- [ ] 对照网站现有文案准备准确更新，链接与下载产物逐项核对，未完成状态明确保留。
- [ ] 有明确站点入口与发布授权才部署；否则交可审阅材料，记录部署待办，不阻塞其他已完成交付。

## 自审与执行说明

- 六个批次均有对应规格、输入输出、检查和交付；已有服务优先复用。
- 五个关键失效类已分配到具体任务，文件安全与兼容失败不能被总通过率抵消。
- Windows先A/B/C与E本端，Mac再D与E本端；缺一端不伪造两端完成。
- 本次用户要求的是提示词交付，实际产品实现由Cursor按设备接续；本计划复选框尚未执行。
