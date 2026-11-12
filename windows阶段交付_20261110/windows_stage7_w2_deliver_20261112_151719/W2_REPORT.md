# 归档盒 Stage7 W2 — Windows 原生验收报告（六项）

- 交付目录：`windows阶段交付_20261110/windows_stage7_w2_deliver_20261112_151719/`
- 验收日期：2026-11-12
- 结论总览：**六项全部通过，未测 0 项，失败 0 项，生产代码零修改**

## 1. 前置校验

| 项 | 结果 |
| --- | --- |
| 起点分支 | windows-stage7-w1（提交 5cc7e46），从中拉出 windows-stage7-w2 |
| 冻结基线 main（Mac b3ded8c） | 未推送、未修改 |
| `main.py` SHA-256 | `943d647c7e192c3d46bab8b9afb3f5dae6c089cf43b0396b33d761fde0b39e80`（验收前后两次核对一致；含 W1 两处修复：表格 `itemSelectionChanged` 连接、`Qt.CheckState(state)`，未回退） |
| STAGE7_W1_REPORT.md 第 8 节 | 已阅读，按其要求执行 |
| stage7 既有证据 | 未重跑、未改写（本轮全部新开目录/新开子进程） |
| 工作源码 | 仅使用 `windows_stage7_deliver_20261110_214635/归档盒源码/` |
| 运行解释器 | `archivebox_v17_windows_adapt_20261110/.venv`（Python 3.14.7 + PySide6 6.12.0，未升级） |

## 2. 环境

| 项 | 值 |
| --- | --- |
| OS | 原生 Windows 11（无 WSL） |
| Qt 平台 | `windows`（全程未设置 `QT_QPA_PLATFORM=offscreen`） |
| 主屏 | ATNA60HU01-0，物理 2560x1600，系统缩放 150%（原生 dpr=1.5） |
| 副屏 | ARZOPA（竖屏，原生 dpr=1.25；逻辑宽不足，未用作档位载体） |
| 长路径注册表 | `LongPathsEnabled=1`（仅记录，未修改） |
| 隔离 | 每个子进程独立覆盖 `ARCHIVEBOX_RECENT_FILE / APP_DIR / SCHEME_DIR / RESULTS_DIR` 四变量，合成目录夹具，未触碰真实资料 |
| 拖放桌面环境 | 资源管理器 + 真实鼠标/键盘注入（SendInput，非 Qt 事件注入） |

### 环境干扰记录（如实保留）

验收过程中发现高权限桌面宠物 **BongoCat（UnityWndClass，PID 89756）** 持续抢占前台焦点：
导致资源管理器无法发起 OLE 拖拽（探针实测 8 次 AttachThreadInput 强制前台全部被其瞬间抢回），
并使注入的键盘事件（Ctrl+A）时灵时不灵（UIPI 丢弃发往高完整性前台窗口的输入）。
该进程无法从测试进程终止（Access denied）。**由用户手动关闭后，拖放测试一次通过。**
此为环境干扰，非产品缺陷；产品代码未做任何针对性改动。

## 3. 六项验收对照表

| # | 项目 | 结论 | 检查项 | 失败 | OS 退出码 | 证据目录 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 原生拖放（资源管理器拖 2 合成文件入待整理清单） | **通过** | 11 | 0 | 0 | `evidence/dragdrop/` |
| 2 | 重复窗口真实打开文件 + 资源管理器定位 | **通过** | 10 | 0 | 0 | `evidence/openreveal/` |
| 3 | >260 字符长路径（扫描/计划/移动/撤销） | **通过** | 10 | 0 | 0（无子进程套件） | `evidence/longpath/` |
| 4 | 文件占用（先证明占用成立，再移动与撤销） | **通过** | 29 | 0 | 0 | `evidence/occupancy/` |
| 5 | DPI 125% / 150% | **通过** | 21 | 0 | 0 | `evidence/dpi/` |
| 6 | 连续 10 次启动关闭 | **通过** | 5 | 0 | 0（10 次子进程退出码全为 0） | `evidence/launch10/` |

汇总：PASS=86，FAIL=0，BLOCK=0，未测=0，缺失=0。
全部六项的结论均为「通过」，不存在把未测写成通过的情形。

## 4. 各项方法与关键证据

### 4.1 第 1 项：原生拖放（w2_dragdrop，11 PASS，exit=0）

- 真实操作链路：`explorer /select,文件1` 打开真实资源管理器窗口 → AttachThreadInput
  强制前台（重试验证）→ SendInput 注入真实系统鼠标/键盘：单击文件区第二行 →
  真实 Ctrl+A 全选 → 从第二行按下、小步越阈、线性推进到待整理清单控件中心释放。
  拖拽由资源管理器自己发起（OLE `DoDragDrop`），**未使用任何 Qt 事件注入/桩**。
- 事件级证据：app 级事件探针记录到 `DragEnter → DragMove(×N) → Drop` 全链路
  到达生产控件 `_DropListWidget`（`QWidget`），释放点 `WindowFromPhysicalPoint`
  命中对话框自身 HWND，`GetCursorPos=(1512,503)` 与目标释放点 (1513,504) 一致。
- 断言证据：拖放后清单恰 +2 且恰为两个合成文件（`DD2_1`）；按清单扫描
  `scope=intake total=2`（`DD3_1`）；计划恰含这两个文件（`DD3_2`）；
  范围外文件不在计划、三文件原位且 SHA-256 不变（`DD3_3`）；
  三张截图非空：资源管理器定位 / Ctrl+A 全选高亮 / 清单+2（`DD4_1`）。
- 截图色带证明 Ctrl+A 真实生效（`DD1_2`）：初始选中行 37px → 全选后 85px
  （≥1.5×初始行高），暗色主题下以探针实测阈值（色差>60、行占比>0.12）判定。

### 4.2 第 2 项：重复窗口真实打开 + 定位（w2_openreveal，10 PASS，exit=0）

- 副本组 2 份相同内容文件 → 核对重复窗口 → `btn_open` 走真实
  `QDesktopServices.openUrl`（未替换）：系统默认程序（记事本
  Microsoft.WindowsNotepad_11.2607.14.0）窗口标题含目标文件名（`OR2_2`），
  目标文件 SHA 不变、窗口关闭（`OR2_3`）。
- `btn_reveal` 走真实 `explorer /select,`（未替换）：捕获新
  `CabinetWClass` 窗口标题「w2打开定位样例目录甲 - 文件资源管理器」（`OR3_1`）
  及选中状态截图（`OR3_2`）。
- 进程级证据（note，尽力捕获）：第二次运行成功捕获 Notepad.exe 进程命令行
  完整含目标路径。Store 版记事本可能复用进程，故窗口级+截图级为主证据。
- 副本不受影响（`OR4_1`）。

### 4.3 第 3 项：长路径（w2_longpath，10 PASS）

- 夹具 289 字符路径（普通 API 建立成功，`fixture_mode=normal`），注册表
  `LongPathsEnabled=1` 仅记录（`LP0_1`）。
- 扫描发现长路径文件（`LP2_1`）→ 计划含长/短两项且长路径项有目标（`LP3_1/3_2`）
  → 移动成功 2 项、目标位置 SHA-256 与移动前一致（`LP4_1/4_2/4_3`）
  → 撤销后全量清单（路径+大小+SHA-256）与移动前完全一致 diff={}（`LP5_1`）。
- 本套件为同进程直测（无子进程），无独立子进程退出码，套件整体 exit=0。

### 4.4 第 4 项：文件占用（stage5 套件复跑，29 PASS，exit=0）

- 先证明占用成立：无 `FILE_SHARE_DELETE` 句柄占住源文件（winerror=32），
  移动时该项 `failed`、源文件保留、目标未创建（未被覆盖误删）；
  未占用文件单独列出并成功 `moved`；释放句柄后撤销完整、
  清单 SHA-256 一致；另含名称边界 N1–N4。

### 4.5 第 5 项：DPI（w2_dpi，21 PASS，exit=0）

- 档位实现（如实标注）：150% 用系统真实缩放（主屏原生 dpr=1.5，无覆盖变量）；
  125% 因修改系统缩放需注销，采用进程级精确模拟
  `QT_ENABLE_HIGHDPI_SCALING=0 + QT_SCALE_FACTOR=1.25`（实测 dpr=1.25）。
- 每档 7 项检查 + EXIT：主窗口/预览表/确认框/历史窗口截图齐全，
  主要按钮与表格列几何可完整点到（表头按几何判定——`QHeaderView.visibleRegion()`
  在滚动区视图内恒为空是 Qt 实现特性，探针实测确认，越界列可横向滚动到达）。
- 探针子进程记录两屏原生 dpr（`DPI0_1`），方法说明（`DPI0_2`）。

### 4.6 第 6 项：10 次启动关闭（launch_exit_10x，5 PASS，exit=0）

- 10 次独立子进程：各自 `app.exec()` 后 `shutdown_widgets()`，
  OS 退出码 `[0,0,0,0,0,0,0,0,0,0]`（`L0_1`），无超时、无遗留 QThread（`L0_3`），
  每次耗时 1191–1289 ms（`L0_4`）。

## 5. 测试装置迭代记录（非产品修复）

本轮六项均通过且**生产代码零修改**（main.py SHA-256 保持不变），以下修复全部发生在
W2 测试脚本自身（首版拖放测试 0/11 失败的根因，如实记录）：

1. `MOUSEINPUT` 结构体漏了 `mouseData` 字段 → `sizeof(INPUT)=32≠40`，
   `SendInput` 返回 0 且 `GetLastError=87`，鼠标键盘全部注入失败（探针定位）。
2. `GetSystemMetrics` 在 per-monitor DPI 感知进程返回逻辑分辨率（1707x1067），
   SendInput 绝对坐标归一化分母须用物理分辨率（2560x1600，QScreen 逻辑宽×dpr）。
3. `grabWindow` 的 pixmap 坐标即物理像素（实测 pm.width=2560），色带检测不得除以 DPR。
4. 点击/拖拽 x 坐标 300 落在导航窗格（文件区 x≥400），改为 560。
5. 点已选中项会触发 Win11 延迟重命名吞掉后续拖拽，改为单击未选中的第二行。
6. `SetForegroundWindow` 静默失败（BongoCat 抢焦点），改 AttachThreadInput 强制前台
   并重试验证。
7. 拖拽按下后先小步越阈（4px×5 步）再线性推进。

拖放测试历次 OS 退出码：首版（0/11 失败）exit=1（装置 bug：截图存在性检查未保护）；
修复后正式运行 exit=0，11/11 通过。产品无修复，故无「修复前后退出码对照」。

## 6. 复现命令

统一解释器：`archivebox_v17_windows_adapt_20261110/.venv/Scripts/python.exe`，
工作目录：`归档盒源码/tests/`，环境变量 `ARCHIVEBOX_TEST_BASE`（合成运行目录）与
`ARCHIVEBOX_RESULTS_DIR`（本交付 `evidence/<项>/`）由各命令行设置：

```
python tests/windows_stage7_w2_longpath.py            # 第3项
python tests/windows_stage5_names_occupancy.py        # 第4项
python tests/windows_launch_exit_10x.py               # 第6项
python tests/windows_stage7_w2_dpi.py                 # 第5项
python tests/windows_stage7_w2_open_reveal.py         # 第2项
python tests/windows_stage7_w2_dragdrop.py            # 第1项（真实鼠标操作会短暂接管屏幕）
```

## 7. 证据清单

- 27 个证据文件（results.json ×6、截图 ×15、清单/运行 JSON ×6）的 SHA-256
  见同目录 `EVIDENCE_INDEX.txt`。
- 六个 `results.json` 汇总：w2_longpath 10 PASS / s5_names_occupancy 29 PASS /
  launch_exit_10x 5 PASS（10 次退出码全 0）/ w2_dpi 21 PASS /
  w2_openreveal 10 PASS / w2_dragdrop 11 PASS。

## 8. 提交说明

- 分支：`windows-stage7-w2`（自 `windows-stage7-w1`@5cc7e46 拉出；经 GitHub API
  blob→tree→commit→ref 上传，本地 .git 被系统清理进程反复移除故走 API 直传）。
- 提交内容：W2 新增四个测试脚本 + 本报告与证据索引；
  不含 `.venv/`、`__pycache__/`、个人资料、运行数据库、`recent_spaces.json`。
- 未测原因：无（六项全测）。
