# 归档盒 V1.6 — Windows 开发交接说明

> 本轮（V1.6）在 macOS（arm64，Python 3.14.8，PySide6 6.12.0）上完成。
> 以下 Windows 内容均为**设计与步骤说明**，未在原生 Windows 上执行，
> 统一标记为「待 Windows 验证」。WSL 结果不能替代原生 Windows 桌面结果。

## 一、可移植性盘点（现有代码）

### 已经可移植的部分

| 项目 | 现状 |
|---|---|
| 运行时 | Python 3.14 + PySide6 6.12，均有 Windows x64 wheel |
| 界面 | 纯 Qt Widgets，无 macOS 专属控件 |
| 路径拼接 | 生产代码统一使用 `os.path.join` / `os.sep`，未写死 Mac 绝对路径 |
| 相对路径比较 | `classification_config._norm_rel` 同时接受 `/` 与 `\` |
| 根目录规范化 | `main._norm_root` 使用 `realpath + abspath`，不做大小写假设 |
| 文件管理器定位 | `main._reveal_in_file_manager` 已有 `win32 → explorer /select,` 分支 |
| 配置目录 | `.organizer` 用普通目录名，Windows 下同样可用 |
| SQLite | 标准库 `sqlite3`，路径由 `os.path.join` 生成 |
| 原子保存 | `tempfile.mkstemp + fsync + os.replace`，Windows 支持 `os.replace` |
| 方案文件 | V1.6 新增格式统一使用 `/` 分隔的相对路径与 UTF-8 编码 |
| 方案库位置 | `scheme_store.library_dir()` 在 Windows 使用 `%APPDATA%\归档盒\schemes` |
| 新测试 | `tests/test_v16_scheme_preview.py`、`run_isolated.py`、`exit_cleanup_check.py` 使用动态项目定位与系统临时目录 |

### 需要 Windows 侧处理的部分

1. **启动入口**
   - 现有 `启动归档盒.command` 是 macOS（zsh）脚本。
   - Windows 需要新增 `启动归档盒.bat`（或 `.ps1`），逻辑保持一致：
     从脚本位置定位 `归档盒源码`、检查 `.venv\Scripts\python.exe`、
     缺少时提示 `python -m venv .venv` 与 `pip install -r requirements.txt`，
     然后运行 `main.py`。
2. **旧测试夹具的绝对路径**
   - V1.0–V1.5 的部分测试仍写死
     `SRC = "/Users/ypjn/Desktop/NEU/大二上/aigc2/归档盒源码"` 与 `/tmp/...`。
   - Windows 上应改为：
     ```python
     SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
     BASE = os.path.join(tempfile.gettempdir(), "_archive_box_verify")
     ```
   - V1.6 新增测试已按此方式编写，可作为改造范例。
3. **长路径**
   - Windows 默认 260 字符限制可能影响深层课程目录。
   - 建议在 Windows 验证时使用 `\\?\` 前缀或启用系统长路径策略；
     当前代码未做专门处理，标记「待 Windows 验证」。
4. **文件占用与删除**
   - Windows 不能在文件被占用时删除/替换。
   - 现有流程已保证：后台线程结束后才关闭 SQLite；保存配置使用
     `os.replace`；撤销/恢复前先核验。仍需在 Windows 上验证
     「历史窗口打开时撤销」「关闭等待期间数据库保活」等路径。
5. **权限模型**
   - Windows 没有 Unix 的 `chmod`，`PermissionError` 的触发场景不同。
     扫描阶段的权限错误提示、受保护目录跳过逻辑需实测。
6. **隐藏文件与项目识别**
   - `.git`、`.DS_Store` 等规则在 Windows 仍适用；但 `desktop.ini`、
     `Thumbs.db` 是否需要额外忽略，标记「待 Windows 验证」。
7. **字体与 HiDPI**
   - Qt 6 在 Windows 使用系统 DPI 缩放；需验证预览表格列宽与
     方案库表格在 125%/150% 缩放下不截断。
8. **退出清理**
   - V1.6 新增 `main.shutdown_widgets()`（应用存活时确定性销毁顶层窗口）。
     macOS 上已通过外层运行器连续验证；Windows 需确认无额外崩溃。

## 二、Windows 原生接续步骤

以下命令在 PowerShell 中执行；路径按实际项目位置调整。

1. 安装 Python 3.14 x64（勾选 “Add python.exe to PATH”）。
2. 进入项目根目录并创建虚拟环境：
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
4. 无界面基础验证（不影响桌面，使用 offscreen）：
   ```powershell
   $env:QT_QPA_PLATFORM = "offscreen"
   .venv\Scripts\python.exe tests\test_v16_scheme_preview.py
   .venv\Scripts\python.exe tests\test_v15_long_term_rules.py
   .venv\Scripts\python.exe tests\test_v14_single_file.py
   .venv\Scripts\python.exe tests\test_v13_config.py
   .venv\Scripts\python.exe tests\test_v12_history_task.py
   ```
5. 外层运行器（同时记录断言、脚本自报退出码、系统真实退出码）：
   ```powershell
   $env:QT_QPA_PLATFORM = "offscreen"
   .venv\Scripts\python.exe tests\run_isolated.py tests\test_v16_scheme_preview.py --repeat 5
   ```
6. 原生桌面检查（不用 offscreen）：
   ```powershell
   .venv\Scripts\python.exe tests\desktop_check_v15_long_term.py
   ```
7. 新建 `启动归档盒.bat` 后双击验证中文+空格路径。

## 三、Windows 基础验证清单（待 Windows 验证）

- [ ] `main.py` 能打开主窗口，中文路径与空格路径均正常。
- [ ] 扫描、生成预览、勾选、筛选、目标树、执行、撤销全流程可用。
- [ ] 单文件修正、保存为长期规则、规则试运行（后台线程）不卡界面。
- [ ] 方案导出/导入/合并/应用/恢复上一次配置可用。
- [ ] 方案库写入 `%APPDATA%\归档盒\schemes`，不写入源码目录。
- [ ] 历史窗口、导出 CSV/JSON、文件定位（explorer /select）。
- [ ] 进程正常退出码为 0，关闭等待期间无线程/数据库泄漏。
- [ ] 深层长路径与只读文件行为符合预期。
- [ ] 安装包/启动脚本不依赖任何 macOS 路径。

## 四、可直接交给 Windows 开发代理的接续提示词

```text
你在 Windows（原生，不是 WSL）上继续开发「归档盒」。

项目位置：<Windows 项目根目录>
源码：归档盒源码/（Python 3.14 + PySide6 6.12，SQLite，纯 Qt Widgets）
文档：归档盒-过程记录.md、归档盒-项目设计/试用说明_V1.3规则配置版.md、
      归档盒-项目设计/Windows开发交接_V1.6.md

先做：
1. 阅读上述文档与 归档盒源码/requirements.txt，确认依赖与现有行为。
2. 按 Windows开发交接_V1.6.md 第二节创建 .venv 并安装依赖，
   用 offscreen 跑 test_v16_scheme_preview.py 与 test_v15_long_term_rules.py，
   记录断言统计、脚本自报退出码、系统真实退出码（用 tests/run_isolated.py）。
3. 若失败，先定位是路径/编码/权限/长路径/文件占用中的哪一类，
   不要通过跳过测试、放宽断言或忽略返回码来“通过”。

随后实现：
- 启动归档盒.bat（从脚本位置定位源码与 .venv，支持中文/空格路径，
  缺依赖时给出明确修复指引，不自动升级依赖）。
- 把旧测试夹具的写死路径改为
  os.path.dirname(os.path.dirname(os.path.abspath(__file__))) 与 tempfile。
- 修复 Windows 原生失败项，并保留修复前后日志。

必须保持的语义：
- 保护目录独立生效；临时修正 > 长期规则 > 原有推荐映射；
- 配置按根目录隔离；旧历史快照不变；
- 试运行与正式计划共用分类/路径构造，试运行不移动文件、不算 SHA；
- 后台任务使用配置快照并拒绝迟到结果；
- 关闭等待期间数据库与窗口保活，线程结束后再清理；
- 方案库不得写入源码目录或扫描根目录。

交付：改动文件、Windows 实测命令与退出码、截图/日志、未测项。
没有实际执行证据的项目一律标记「待 Windows 验证」。
```

## 五、明确未完成的部分

- 未创建 Windows 启动脚本（`.bat`/`.ps1`）。
- 未在 Windows 上运行任何测试或界面。
- 未处理 Windows 长路径（>260 字符）的专门适配。
- 未验证 Windows 权限模型、文件占用、DPI 缩放、`desktop.ini` 等差异。
