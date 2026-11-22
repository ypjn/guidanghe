# 归档盒后续接续任务：A–F

本轮由用户要求继续编写多步 Cursor 开发提示词。基线是 `cursor/batches-05-07-7481` 的 `6ccd97a678afa8367b55123d6ddd8c0064722bd8`，当前交接分支在此基础上只增加提示词与计划。

## 使用

- **Windows Cursor**：先发 [00-START-WINDOWS.txt](00-START-WINDOWS.txt)，按 A→B→C→E 的 Windows 部分执行，提交真实证据并回传。
- **Mac Cursor**：接收 Windows 提交后发 [D-MAC-DMG.txt](D-MAC-DMG.txt)，继续 E 的 Mac 部分；两端证据齐备再准备 F。
- **Linux 云端执行者**：可做界面接入、隔离回归和构建准备；Windows真盘/桌面、Mac原生/DMG保持待测，交给对应设备执行，不以修改平台标志替代。

| 批次 | 交付目标 |
| --- | --- |
| [A](A-BACKUP-UI.txt) | 主窗口可进入备份管理，完成预览、版本列表、校验、备份、恢复、取消与退出 |
| [B](B-WINDOWS-VOLUME.txt) | Windows真实外置盘只读登记、离线查询、安全重连及路径/权限验证 |
| [C](C-WINDOWS-PACKAGE.txt) | 原生构建并验收独立运行的Windows包，修运行目录、依赖、启动器与退出码 |
| [D](D-MAC-DMG.txt) | 接回共享改动，完成Mac兼容复验、真实.app和DMG |
| [E](E-INDEPENDENT-ACCEPTANCE.txt) | 固定候选，做文件安全、取消并发、迁移和真实入口独立复核与返修 |
| [F](F-RELEASE-PAGE.txt) | 按真实发布结果准备官网文案、下载信息、支持矩阵与试用步骤 |

A–C是这台Windows上的连续执行任务。D需要Mac，不能因为设备不匹配伪造已完成。E可以先做当前设备可验证部分，最终保留完整跨平台矩阵。F先准备可审阅发布材料，不猜测网站工程和部署入口。

[PLAN.md](PLAN.md)给出任务依赖、接口与检查项。每批读取自己的完整提示词，完成必要验证和修复后再推进；已经实现的索引、卷身份和备份服务优先复用，不重建平行系统。

本目录是待执行指令，不是实现或验收结果。旧 `windows-stage7-w1/w2` 是 V1.7 证据，不能算作本轮 V1.8.1 原生验收。
