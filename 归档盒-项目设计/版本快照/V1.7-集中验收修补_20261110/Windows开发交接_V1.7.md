# 归档盒 V1.7 — Windows 原生开发交接说明

> 本版基于 macOS（arm64，Python 3.14.8，PySide6 6.12.0）完成功能冻结与集中验收。
> **以下 Windows 内容均为设计与步骤说明，未在原生 Windows 上执行**，统一标记
> 「待 Windows 验证」。WSL / Wine 的结果不能替代原生 Windows 桌面结果。
>
> V1.6 交接文档（`Windows开发交接_V1.6.md`）中的可移植性盘点与基础清单仍然有效，
> 本文件补充 V1.7 的新增模块、验收结论与原生验证项目。

## 一、交接包内容

```
归档盒_V1.7_Windows开发交接/
├── README_Windows交接.md          本文件
├── 归档盒源码/
│   ├── *.py                       生产代码（含 V1.7 重复查看、日常入口）
│   ├── requirements.txt           PySide6==6.12.0
│   └── tests/                     自动化测试与验收脚本
├── 文档/
│   ├── 归档盒-过程记录.md
│   ├── 试用说明_V1.3规则配置版.md
│   ├── Windows开发交接_V1.6.md
│   ├── Windows开发交接_V1.7.md
│   ├── 冻结基线_V1.7.md
│   └── 集中验收报告_V1.7_20261110.md
├── 验收证据/
│   ├── results_*.json             本轮关键套件结果
│   └── 截图/                      Mac 桌面检查截图（程序化，不是真人试用）
└── 真人试用清单_V1.7.md           由使用者本人填写
```

**不包含**：`.venv`、`__pycache__`、`.DS_Store`、运行数据库
（`.organizer/history.db`）、本机最近整理空间记录（`recent_spaces.json`）、
个人资料与任何真实 NEU 文件。

## 二、环境与启动

1. 安装 Python 3.14 x64（勾选 “Add python.exe to PATH”）。
2. 在项目根目录创建虚拟环境并安装依赖：
   ```powershell
   cd <项目根目录>
   py -3.14 -m venv 归档盒源码\.venv
   归档盒源码\.venv\Scripts\python.exe -m pip install --upgrade pip
   归档盒源码\.venv\Scripts\python.exe -m pip install -r 归档盒源码\requirements.txt
   ```
3. 启动：
   ```powershell
   cd 归档盒源码
   .venv\Scripts\python.exe main.py
   ```
4. 退出：关闭主窗口。程序会等待后台任务结束、关闭 SQLite 后再退出；
   正常退出码应为 0。

## 三、V1.7 新增/变化（Windows 侧需要了解）

| 模块 | 作用 | 与平台相关的注意点 |
|---|---|---|
| `duplicate_report.py` | 内容重复 / 同名不同内容 / 待核对的分组与导出 | 纯逻辑；SHA-256、`os.stat().st_mtime_ns` |
| `duplicate_dialog.py` | 重复文件窗口、搜索筛选、打开/定位/复制路径、CSV/JSON 导出 | `QDesktopServices`、文件管理器定位 |
| `recent_spaces.py` | 最近整理空间（本机状态） | Windows 落在 `%APPDATA%\归档盒\recent_spaces.json` |
| `intake_model.py` | 待整理清单、去重与父子包含、范围解析 | `os.path.commonpath` 的盘符/大小写行为需实测 |
| `daily_entry_dialogs.py` | 最近空间与待整理清单窗口、拖放 | Windows 资源管理器拖放的 URL 列表需实测 |
| `main.py` | 入口与主窗口；V1.7 增加重复查看、范围扫描 | `atexit` 兜底销毁控件，避免退出期析构崩溃 |
| `tests/run_isolated.py` | 记录断言、自报退出码、系统真实退出码 | `subprocess` 返回码语义一致；崩溃映射为负值/非零 |
| `tests/acceptance_v17_scale.py` | 真实副本规模验收 | 默认路径为 macOS 临时目录，改 `--root` 即可 |

### 退出清理（V1.7 验收修补）

- 生产入口在 `app.exec()` 后显式调用 `shutdown_widgets()`；
- 同时在模块导入时注册 `atexit.register(shutdown_widgets)` 作为兜底，
  防止异常路径或测试脚本跳过显式清理时，PySide 在解释器最终化阶段
  按错误顺序析构控件导致 SIGSEGV；
- macOS 验证：显式清理 10/10、非显式清理 15/15，操作系统退出码均为 0
  （修补前非显式清理 10 次中 2 次 SIGSEGV）。
- **待 Windows 验证**：Windows 上同样组合连续运行 ≥10 次，真实退出码均为 0。

## 四、Windows 基础验证清单（待 Windows 验证）

- [ ] `main.py` 打开主窗口；中文、空格、深层路径均正常。
- [ ] 扫描 / 计划 / 预览 / 勾选 / 筛选 / 目标树 / 执行 / 撤销全流程可用。
- [ ] **原生拖放**：把单个文件、子目录、重复项、父子目录拖入待整理清单，
      行为与 Mac 一致；根目录外来源得到明确提示。
- [ ] **最近整理空间**：保存/重启恢复/固定/移除；不存在的目录不会被自动创建；
      记录位于 `%APPDATA%\归档盒\recent_spaces.json`。
- [ ] **重复文件核对**：内容重复、同名不同内容、待核对/失败分组正确；
      在程序外修改文件后重开窗口会重新核对变化项；打开/定位/复制路径/导出
      不改变文件内容和副本数量。
- [ ] 整理、撤销、重新扫描后旧重复结果失效，不显示过期路径。
- [ ] 方案库写入 `%APPDATA%\归档盒\schemes`，不写入源码目录；
      方案标识校验与目录边界有效。
- [ ] 文件管理器定位使用 `explorer /select,`，中文/空格路径正确。
- [ ] 历史窗口、导出 CSV/JSON、撤销/恢复。
- [ ] 长路径（>260 字符）、只读文件、文件占用时的移动/撤销/恢复行为。
- [ ] 125% / 150% DPI 下表格列宽、按钮与对话框不截断。
- [ ] 关闭等待期间无新任务启动；线程结束后才关闭数据库；
      连续 10 次启动/退出，真实退出码均为 0。

## 五、已知问题与非阻断改进（Mac 验收结论）

**阻断问题（已修复）**：解释器退出阶段偶发 SIGSEGV（非显式清理路径）。
已用 `atexit.register(shutdown_widgets)` 兜底，显式与非显式清理均复跑通过。

**非阻断、后续处理**：

1. 重复文件窗口只读：不提供删除、合并、自动勾选或“释放空间”。
2. 待整理范围扫描复用整空间扫描后过滤，不是增量扫描引擎。
3. 不提供跨根目录执行队列；不同空间需分别整理。
4. 最近空间最多保留 30 条，超出按非固定、最久未打开优先丢弃。
5. 撤销只恢复路径与内容；修改时间会变为撤销时刻，权限位/ACL/扩展属性
   不在核对范围。
6. 独立清单不记录目录，撤销后允许残留空父目录。
7. V1.0–V1.5 的部分测试仍写死 Mac 绝对路径（`/Users/...`、`/tmp`）；
   在 Windows 上需改为 `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`
   与 `tempfile.gettempdir()`。
8. `run_isolated.py` 的 `crash_at_exit` 依赖脚本自报统计；
   无统计行的脚本（如 `exit_cleanup_check.py`）需直接看系统退出码。
9. 未制作安装包、代码签名或自动更新。
10. 未进行 16 GB 整批移动与十万文件级压力验收；本轮为 11.08 GB / 3.78 万文件。

## 六、Windows 接续步骤

1. 解压交接包，阅读 `文档/集中验收报告_V1.7_20261110.md` 与
   `文档/冻结基线_V1.7.md`，确认基线。
2. 按第二节创建 `.venv` 并安装 `PySide6==6.12.0`（不要擅自升级）。
3. 无界面回归（PowerShell）：
   ```powershell
   $env:QT_QPA_PLATFORM = "offscreen"
   .venv\Scripts\python.exe tests\test_v17_duplicates.py
   .venv\Scripts\python.exe tests\test_v17_daily_entry.py
   .venv\Scripts\python.exe tests\test_v17_combined.py
   .venv\Scripts\python.exe tests\test_v16_scheme_preview.py
   .venv\Scripts\python.exe tests\run_isolated.py tests\test_v17_duplicates.py --repeat 3
   ```
   记录断言统计、脚本自报退出码、操作系统真实退出码；
   任何非零退出或退出期崩溃都必须如实记录，不得跳过或放宽断言。
4. 新增 `启动归档盒.bat`：从脚本位置定位 `归档盒源码` 与 `.venv\Scripts\python.exe`，
   支持中文/空格路径；缺依赖时给出明确修复指引，不自动升级依赖。
5. 把旧测试夹具的写死路径改为动态定位与系统临时目录。
6. 按第四节逐项做原生验证，失败项先定位类别（路径/编码/权限/长路径/文件占用），
   保留修复前后日志。
7. 修复完成后重新生成 Windows 专属快照与结果，不覆盖 Mac 冻结快照。

## 七、可直接交给 Windows 开发代理的提示词

```text
你在 Windows（原生，不是 WSL）上继续开发「归档盒」。

项目位置：<Windows 项目根目录>
源码：归档盒源码/（Python 3.14 + PySide6 6.12，SQLite，纯 Qt Widgets）
文档：文档/Windows开发交接_V1.7.md、文档/集中验收报告_V1.7_20261110.md、
      文档/试用说明_V1.3规则配置版.md、文档/归档盒-过程记录.md

第一步：
1. 阅读上述文档与 requirements.txt，确认依赖与既有行为。
2. 创建 .venv，安装 PySide6==6.12.0（不升级）。
3. 用 offscreen 依次运行 test_v17_duplicates.py、test_v17_daily_entry.py、
   test_v17_combined.py、test_v16_scheme_preview.py，并用 run_isolated.py
   连续运行 3 次，记录断言统计、脚本自报退出码、操作系统真实退出码。
4. 失败时先定位路径/编码/权限/长路径/文件占用类别，不要通过跳过测试、
   放宽断言或忽略返回码来“通过”。

随后：
- 新增 启动归档盒.bat（中文/空格路径，缺依赖给出修复指引）。
- 把旧测试的 Mac 绝对路径改为动态定位 + 系统临时目录。
- 完成 Windows开发交接_V1.7.md 第四节的 12 项原生验证，
  重点：拖放、最近空间、重复核对、长路径、文件占用、DPI、退出清理。
- 保留修复前后日志；生成 Windows 专属快照，不覆盖 Mac 快照。

必须保持的语义：
- 保护目录独立生效；临时修正 > 长期规则 > 原有推荐映射；
- 配置按根目录隔离；旧历史快照不变；
- 试运行与正式计划共用分类/路径构造，试运行不移动文件、不写历史；
- 撤销按实际记录恢复，不按当前规则重算过去操作；
- 重复文件查看只读，不删除/合并/自动勾选；
- 一份计划只属于一个整理根目录，不静默改变根目录。
```
