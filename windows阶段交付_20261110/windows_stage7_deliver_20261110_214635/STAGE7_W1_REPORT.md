# 归档盒 V1.7 — Windows 阶段 stage7：W1 已有功能原生验收报告

- 轮次：stage7（W1 已有功能的 Windows 原生验收，不新增产品功能）
- 候选基线：stage6b 最终源码（顾问已独立验收通过：89 项哈希一致、补丁重建 74 项源码一致、19 PASS / 0 FAIL / 0 BLOCK、退出码 0）
- 运行环境：Windows，Python 3.14.7，PySide6 6.12.0，Qt 6.12.0，Qt 平台插件 `windows`，2 块屏幕
- 运行根（本轮新建）：`_s7run_20261110_w1_final`
- 交付目录：`windows_stage7_deliver_20261110_214635`
- 旧基线、旧交付、顾问证据未改动

## 0. 结论速览

| 项目 | 结果 |
| --- | --- |
| 父套件 `tests/windows_stage7_w1.py` | 20 PASS / 0 FAIL / 0 BLOCK / 0 UNTESTED，套件自报与进程真实退出码均为 0 |
| 子进程（6 个） | 全部 `exit_code = 0`，无超时，无 stderr 异常 |
| 子断言合计 | 147 条（**断言条数不等于功能覆盖数**，覆盖结论见第 1 节） |
| 生产缺陷 | 发现 2 个，均已修复并回归（第 5 节），补丁正反向往返校验通过 |
| 阻断 | 无未修复阻断项 |
| 是否可进入剩余 W2 平台差异验收 | **可以**（前置条件见第 8 节） |

## 1. 已有功能覆盖表

状态口径：
- **A｜当前源码已有直接证据**：本轮在 Windows 原生 Qt 界面用真实控件操作，并断言控件/计划/配置/数据库/文件结果。
- **B｜只有底层或部分证据**：只验证到调用点、参数或底层逻辑，真实桌面动作未完成。
- **C｜尚未验证**。

| 功能面 | 子项 | 状态 | 本轮证据（检查项 ID） |
| --- | --- | --- | --- |
| 1 长期规则与临时修正 | 临时修改（界面模态表单） | A | `R1-*` |
| | 恢复自动分类 | A | `R2-*` |
| | 长期规则试运行 + 保存（嵌套模态） | A | `R3-*` |
| | 规则生效与逐文件实际目标/依据 | A | `R4-*` |
| | 分类优先级与排序（上移/下移） | A | `R5-*` |
| | 规则条件变化导致缓存失效、计划失效 | A | `R3-*`、`R7-*` |
| | 启停/删除规则（恢复默认配置） | A | `R6-*`、`R7-*` |
| | 执行 1 项 + 撤销 + 清单/SHA-256 一致 | A | `R8-*` |
| 2 方案导入导出与回退 | 保存当前为方案、本地方案库 | A | `S1-*` |
| | 导出当前配置 | A | `S2-*` |
| | 导入方案文件 | A | `S3-*` |
| | 替换应用 + 差异预览 + 影响预览 | A | `S4-*` |
| | 合并应用（冲突与集合结果） | A | `S5-*` |
| | 备份失败必须阻断应用 | A | `S6-*` |
| | 配置回退 | A | `S7-*` |
| | 非法方案标识不得写出方案库 | A | `S8-*` |
| 3 筛选、勾选与执行确认 | 隐藏已选项不被静默改变 | A | `C1-*`、`C2-*` |
| | 批量勾选只作用于可见范围 | A | `C3-*` |
| | 隐藏勾选提醒与确认统计一致 | A | `C4-*` |
| | 取消执行不产生批次与文件变化 | A | `C4-*` |
| | 目标目录树与逐文件目标对应 | A | `C5-*` |
| | 执行统计与实际移动一致 + 撤销清单/SHA | A | `C6-*` |
| 4 最近空间 / 待整理清单 / 范围 | 重启恢复最近空间记录 | A | `Z1-RESTART-RECOVERED` |
| | 不存在目录不自动创建、不可打开 | A | `Z1-MISSING-STATUS`、`Z1-MISSING-NOT-OPENABLE`、`Z1-MISSING-NOT-CREATED` |
| | 固定 / 取消固定 / 移除记录 | A | `Z1-PIN`、`Z1-UNPIN`、`Z1-REMOVE-RECORD-ONLY`、`Z1-RECENT-FILE-CONTENT` |
| | 待整理清单：文件/目录/重复/父子包含/根外来源 | A | `Z2-*` |
| | 指定范围实际计划正确 | A | `Z3-*` |
| | 范围变化使旧计划失效 | A | `Z5-*` |
| | 范围外文件不进入计划 | A | `Z3-EXCLUDED-OUTSIDE`、`Z6-*` |
| | Windows 资源管理器原生拖放 | **C** | 仅做 Qt 事件注入 `Z4-DROP-QT-EVENT`，不代表原生拖放通过 |
| 5 重复核对 | 内容重复分组 | A | `D1-*`、`D2-*` |
| | 同名不同内容分组 | A | `D3-*` |
| | 失败/待核对分组 | A | `D4-*` |
| | 复制路径 | A | `D5-COPY-PATH` |
| | 打开 / 在资源管理器中定位 | **B** | `D5-OPEN-CALLED`、`D5-REVEAL-CALLED`（外部程序唤起被拦截，仅核对调用参数） |
| | CSV / JSON 导出内容 | A | `D6-*` |
| | 外部修改后重开窗口重新核对 | A | `D7-*` |
| | 不删除 / 不合并 / 不自动执行 | A | `D8-*` |
| 6 历史导出 | 界面查询（批次/明细/搜索/状态筛选） | A | `H1-*`、`H2-*` |
| | CSV / JSON 实际内容 | A | `H3-*` |
| | 源目标路径与规则依据 | A | `H1-RULE-BASIS`、`H3-*`、`H4-*` |
| | 导出不按当前规则重算过去目标 | A | `H5-*` |
| 通用 | 扫描期间取消入口 | A（按设计） | 扫描/计划期无取消按钮，本轮未新增（用户指令） |
| 通用 | 跨盘移动、符号链接、超长路径、DPI、规模 | **C** | 本轮不展开（用户指令） |
| 通用 | Mac 端复验 | **C** | 见第 9 节清单 |

## 2. 验证方法与环境隔离

- 全部为 Windows 原生 Qt 界面驱动：`btn.click()`、`table.selectRow()`、`QFormLayout.labelForField` 定位字段、`QDialogButtonBox.button(std)`、`findChildren(QPushButton)`；模态对话框由 `QApplication.activeModalWidget()` + 重复定时器分派（`tests/stage7_children/s7_common.py`）。
- 应用状态隔离：每个子进程独立 `ARCHIVEBOX_RECENT_FILE` / `ARCHIVEBOX_APP_DIR` / `ARCHIVEBOX_SCHEME_DIR` / `ARCHIVEBOX_RESULTS_DIR`，并清除 `QT_QPA_PLATFORM`。
- 数据隔离：每个子进程只使用 `ARCHIVEBOX_TEST_BASE/<name>_root` 下**新建的合成目录**，未触碰任何真实个人资料。
- 断言对象：控件文本/可用性/可见性、计划条目字段、配置文件 JSON 字节、SQLite 动作记录、磁盘文件与 SHA-256。截图仅作辅助。
- 退出码四层口径：L1 子套件自报（不采信）；L2 被测进程真实 OS 退出码（父套件用 `run_child` 记录并复核）；L3 父套件；L4 调用者 `$LASTEXITCODE`。
- 每个实际移动场景都保存移动前清单，撤销后重新生成清单并逐项比较路径、数量与 SHA-256（`R8-*`、`C6-*`）。

## 3. 本轮新增测试与原始证据

| 子套件 | 覆盖功能面 | 断言 | 退出码 | 截图 | 产物 |
| --- | --- | --- | --- | --- | --- |
| `s7_child_rules.py` | 1 长期规则与临时修正 | 30 | 0 | 9 | 5 |
| `s7_child_scheme.py` | 2 方案导入导出与回退 | 18 | 0 | 7 | 7 |
| `s7_child_select.py` | 3 筛选、勾选与执行确认 | 21 | 0 | 4 | 2 |
| `s7_child_scope.py` | 4 最近空间/待整理清单/范围 | 31 | 0 | 4 | 1 |
| `s7_child_dupes.py` | 5 重复核对 | 23 | 0 | 2 | 3 |
| `s7_child_history.py` | 6 历史导出 | 24 | 0 | 2 | 3 |
| `windows_stage7_w1.py` | 父套件（汇总 + 独立复核） | 20 | 0 | — | 证据索引 |

原始证据位置（`evidence/`）：

- `suite/stage7_results.json`：父套件逐项结果、耗时、PID、Python 与平台。
- `suite/stage7_evidence.json`：每个子进程的命令、隔离环境、stdout/stderr 日志路径、真实退出码、断言条数、失败 ID、产物哈希、截图列表、备注。
- `out_<子套件>/`：`child_<label>.py`（实际被执行脚本）、`*.process.json`、`*.stdout.txt`、`*.stderr.txt`、`result.json`（子进程自写的结果文件，父套件重新读取核对条数）、`shots/`。
- 关键截图：`01`–`09`（规则与手动修正/历史）、`10`–`16`（方案中心、差异预览、影响预览、备份失败阻断）、`20`–`23`（勾选、确认、目标树、执行后）、`30`–`33`（最近空间、移除、待整理清单、清单范围）、`40`–`41`（重复核对、外部修改后重核）、`50`–`51`（历史明细、改配置后历史不变）。

## 4. 实际配置、导出文件与文件核对证据

- 规则配置：`out_rules/…/classification_config.json` 断言 `long_term_rules[0]` 的 `match_type/source_path/direction/category/enabled` 与界面保存一致；删除全部规则后配置文件被移除、回到默认分类（`R7-*`）。
- 方案与备份：`out_scheme` 中方案库文件 +1、`config_backups` 备份 +1/-1、`S6-*` 证明备份目录不可建时配置字节完全不变且应用被阻断、`S8-*` 证明 `export_scheme_to("../../evil", …)` 抛 `ClassificationConfigError` 且目标未创建、库列表不变。
- 导出文件：`evidence/out_dupes/duplicates.csv`、`duplicates.json`；`evidence/out_history/history_batch.csv`、`history_batch.json`、`history_batch_after_config_change.csv`。父套件对每个产物重新计算 SHA-256 并写入证据索引。
- 历史不重算：`H5-*` 先改配置使同名源文件的新目标变为 `_已整理/校园事务/待归档/…`，再刷新历史，历史明细与再次导出仍是当时的 `_已整理/项目/讲义存档/…` 与 `long_term:rule-hist-0001`。
- 移动/撤销核对：`R8-*`、`C6-*` 撤销后清单 diff 为空（`missing/extra/changed` 全空），逐文件 SHA-256 与移动前一致。

## 5. 生产修改（2 处，均为 W1 阻断级缺陷）

### 5.1 手动修正入口不可达（`main.py` `_build_ui`）

- 现象：真实用户点选表格一行后，「修正所选文件分类…」按钮始终禁用，手动修正入口在界面上不可达。全源码没有任何表格 `itemSelectionChanged` 连接。
- 修复：在表格创建后补 `self.table.itemSelectionChanged.connect(self._update_button_states)`。
- 修复前证据：`_s7dev/prefix_rules_stdout.txt`（点击行后按钮 `isEnabled()` 为 False）。
- 修复后：`R1-*` 全流程通过。

### 5.2 行内复选框无法勾选（`main.py` `_on_check_changed`）

- 现象：PySide6 6.12 中 `stateChanged` 以 `int` 发出，而 Qt6.4+ 起 `Qt.Checked` 是严格 Python 枚举，`2 == Qt.CheckState.Checked` 为 `False`。结果是用户点击行内复选框只能取消、永远无法勾选（只有「勾选全部」类按钮生效）。
- 修复：`Qt.CheckState(state) == Qt.CheckState.Checked`。
- 修复前证据：`_s7dev/probe_check_before_fix.txt`（`setChecked(True)` 后 `selected_count` 仍为 0，`toggled/stateChanged` 均已触发）。
- 修复后证据：`_s7dev/probe_check_after_fix.txt`（原反例转为 `selected_count = 1`）；直接相关回归 `s7_child_rules.py` 30 项、`s7_child_scheme.py` 18 项、`s7_child_select.py` 21 项全部通过。

### 5.3 补丁与往返校验

- 补丁：`patches/windows_stage7_production_fixes.patch`（2 个 hunk，仅这 2 处）。
- 校验（`git -c core.autocrlf=false apply`）：
  - 反向应用后得到修复前源码，`main.py` SHA-256 = `dffd2de72200a8d6899d7e4beb6858cad97917348bf35cc8237900dcdea816f4`，且只含旧比较式、无新增信号连接；
  - 再正向应用后与服务交付源码逐字节一致，SHA-256 = `943d647c7e192c3d46bab8b9afb3f5dae6c089cf43b0396b33d761fde0b39e80`。

## 6. 最终源码与重建步骤

- 最终源码：`归档盒源码/`（与运行所用源码逐字节一致，已排除 `__pycache__`、`.venv`）。
- 完整重建步骤：见 `REBUILD.md`。

## 7. 同轮证据索引与哈希

- `EVIDENCE_INDEX.json`：对交付目录内全部交付文件（源码、补丁、证据、报告）逐文件记录大小与 SHA-256，并在生成后重新读盘复核。
- 生成脚本输出的复核结果：`verified_files` 与实际文件数一致，`mismatch` 为空。

## 8. 剩余未测项

### 8.1 W1 仍缺直接证据（不阻断）

1. Windows 资源管理器**原生**拖放（含从桌面/资源管理器拖入清单、拖出）：本轮只做 Qt 事件注入（`Z4-DROP-QT-EVENT`，实际走到「直接调用控件 `dropEvent`」路径），不能宣称原生拖放通过。
2. 重复核对窗口的「打开文件」与「在资源管理器中定位」真实外部程序唤起：为避免测试弹出阅读器/资源管理器，本轮替换了 `QDesktopServices.openUrl` 与 `_reveal_in_file_manager`，只核对调用参数与绝对路径。
3. 跨盘移动、符号链接/联接点、超长路径（>260）、高 DPI 缩放、大规模（万级文件）性能：按本轮指令不展开。
4. 迁移真实个人资料：按指令未做，全部使用合成目录。

### 8.2 最短人工操作步骤（用于补齐 8.1 的 1、2 两项）

1. 原生拖放：打开归档盒 → 选择合成根目录 → 点「待整理清单」→ 从资源管理器把 2 个文件拖入清单窗口 → 确认清单条数 +2 → 关闭窗口，点「按清单扫描」→ 确认计划只含这 2 个文件。
2. 打开：打开「核对重复文件」→ 展开任一内容重复组 → 选中一行 → 点「打开文件」→ 应使用系统默认程序打开该文件。
3. 定位：同一窗口 → 点「在资源管理器中定位」→ 资源管理器应打开并选中该文件。

### 8.3 W2 平台差异验收（尚未开始）

- 本轮只做 W1 已有功能的 Windows 原生验收，未新增 W2 功能，也未验证 W2 平台差异项。
- 进入 W2 的前置条件：本报告与补丁经顾问审阅通过；8.1 中 1、2 两项按 8.2 步骤补齐或书面接受为「未测」。

## 9. Mac 复验清单

1. 用 stage6b Mac 源码 + 本补丁重新跑既有 Mac 套件，确认 2 处修复在 Mac 的 PySide6 版本上同样正确（尤其 `_on_check_changed`：旧写法在严格枚举下必定失效，需确认 Mac 侧版本行为并统一为 `Qt.CheckState(state)`）。
2. 手动修正入口：点选一行后按钮立即可用；改选另一行后目标随之更新。
3. 行内复选框：单勾/单取消、筛选后隐藏项保持勾选、批量勾选仅作用可见范围。
4. 目标路径分隔符：Mac 下 `PlanItem.target_rel_path` 的文件名段为 `/`，本轮的 Windows 断言不可直接照搬到 Mac。
5. 历史导出：CSV 首行为批次摘要行、第二行为列名、其后为动作行；`rule_key` 前缀为 `long_term:`。
6. 重复核对导出格式 `archive-box-duplicate-report` 与计数键名保持一致。

## 10. 附：本轮判定口径

- 断言条数（147 + 20）只代表执行过的检查项数量，**功能覆盖数以第 1 节表格为准**。
- 失败先分类为生产缺陷/夹具/环境/未验证；本轮所有失败均已定位为「断言写法或前置状态」问题并修正夹具，另有 2 项定位为真实生产缺陷（第 5 节），修复前证据均保留。
- 未因扫描期没有取消入口而新增按钮；未恢复复制删除兜底；旧基线、旧交付与顾问证据均未改动。
