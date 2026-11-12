# 完整重建与复跑步骤（stage7 W1 交付）

本交付源码 = stage6b 最终源码 + `patches/windows_stage7_production_fixes.patch`（2 处生产修复）。

## 1. 环境

| 项 | 本轮实际值 |
| --- | --- |
| 操作系统 | Windows（x64） |
| Python | 3.14.7 |
| PySide6 / shiboken6 | 6.12.0 |
| Qt | 6.12.0（平台插件 `windows`，2 块屏幕） |

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install PySide6==6.12.0
```

`归档盒源码/requirements.txt` 已随源码交付，可直接 `pip install -r requirements.txt`。

## 2. 从 stage6b 基线重建（若需从零验证补丁）

```powershell
# 1) 取 stage6b 最终源码到 <work>\归档盒源码
# 2) 应用补丁（在源码根目录执行，即 main.py 所在目录）
cd <work>\归档盒源码
git -c core.autocrlf=false apply -p1 <交付目录>\patches\windows_stage7_production_fixes.patch
# 3) 核对 main.py 哈希，应与交付一致
python -c "import hashlib;print(hashlib.sha256(open('main.py','rb').read()).hexdigest())"
# 期望：943d647c7e192c3d46bab8b9afb3f5dae6c089cf43b0396b33d761fde0b39e80
```

反向核对（可选，验证补丁只包含这 2 处）：

```powershell
git -c core.autocrlf=false apply -p1 --reverse <交付目录>\patches\windows_stage7_production_fixes.patch
python -c "import hashlib;print(hashlib.sha256(open('main.py','rb').read()).hexdigest())"
# 期望（修复前）：dffd2de72200a8d6899d7e4beb6858cad97917348bf35cc8237900dcdea816f4
```

## 3. 直接使用交付源码复跑

```powershell
$src = "<交付目录>\归档盒源码"
$base = "<新目录>\stage7_rerun_<时间戳>"        # 每次复跑用全新目录
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
$env:ARCHIVEBOX_TEST_BASE = $base
& "<venv>\Scripts\python.exe" "$src\tests\windows_stage7_w1.py"
$code = $LASTEXITCODE
"退出码=$code"
```

- 父套件为每个子套件注入隔离环境（`ARCHIVEBOX_SRC` / `ARCHIVEBOX_RESULTS_DIR` / `ARCHIVEBOX_RECENT_FILE` / `ARCHIVEBOX_APP_DIR` / `ARCHIVEBOX_SCHEME_DIR` / `S7_OUT` / `PYTHONPATH`），不需要手工设置。
- 期望结果：`PASS 20 / FAIL 0 / BLOCK 0 / UNTESTED 0`，真实退出码 `0`；`<base>\suite\stage7_results.json` 与 `stage7_evidence.json` 生成。
- 若需单跑某个功能面：

```powershell
$env:ARCHIVEBOX_SRC = $src
$env:ARCHIVEBOX_TEST_BASE = $base
$env:ARCHIVEBOX_RESULTS_DIR = "$base\out_select"
$env:S7_OUT = "$base\out_select\result.json"
$env:ARCHIVEBOX_RECENT_FILE = "$base\recent.json"
$env:ARCHIVEBOX_APP_DIR = "$base\appdata"
$env:ARCHIVEBOX_SCHEME_DIR = "$base\appdata\schemes"
& "<venv>\Scripts\python.exe" "$src\tests\stage7_children\s7_child_select.py"
```

可替换脚本名：`s7_child_rules.py`、`s7_child_scheme.py`、`s7_child_select.py`、`s7_child_scope.py`、`s7_child_dupes.py`、`s7_child_history.py`。

## 4. 注意事项

- 每次复跑前结束残留进程（残留进程会占用 `result.json` 与隔离目录）：
  `Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*<venv>*" } | Stop-Process -Force`
- 子套件会在自己的根目录写入 `.organizer`（SQLite）、`config_backups`、`_已整理`，请始终使用全新 `ARCHIVEBOX_TEST_BASE`。
- 所有断言都基于真实控件/计划/配置/数据库/文件结果；截图仅供辅助阅读。
- 拖放检查为 Qt 事件注入，不代表 Windows 原生拖放已通过。
