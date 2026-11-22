# 归档盒独立复核（Windows 部分）

审查者：同一开发者自审（无独立执行者）。
日期：2026-11-22  
平台：Windows 11 家庭版 10.0.26200，Python 3.14.7 / PySide6 6.12.0  
分支：`product/desktop-finish-win`

## 固定候选

固定源码提交：`af1a5d21ffd1b75435adeef032b4b88198a7eb22`（构建身份写入 `app/build_identity.json`）。  
Windows ZIP：`打包/dist/归档盒-1.8.1-windows-x64.zip`  
SHA-256：`95eee7d2c5db1e649ddea46027fe9987c985a3c6a0b2efde6952491f2f737305`

## 覆盖矩阵

| 项 | Windows | 说明 |
| --- | --- | --- |
| 备份 UI 入口 / 预览作废 / 取消 | 离屏通过 | `test_product_backup*.py` EXIT=0 |
| 卷序列号 / junction 越界 | 夹具通过 | `test_product_volume.py` EXIT=0 |
| 真盘登记→离线→重连 | 待测 | 等用户指定只读资料目录与可写测试目录 |
| 独立 ZIP 启动 / smoke-gui | 通过 | `归档盒.exe --product-acceptance smoke-gui` EXIT=0 |
| 包内不写日志 | 通过 | 运行后包根无 `启动日志.txt` |
| Mac DMG / 原生 | 未测 | 交 Mac 执行 D |
| OCR 捆绑 | 未纳入便携核心包 | `requirements-windows-portable.txt` 仅 PySide6+pypdf |

## 成立问题与修复

| ID | 问题 | 复现 | 修复提交 |
| --- | --- | --- | --- |
| W1 | 备份无桌面入口，预览/校验不可取消 | UI 测试先失败再接入 | `292df56` |
| W2 | Windows 上 `/abs` 恢复路径未拒绝 | `test_validate_restore_paths` | `292df56` |
| W3 | 子目录卷标为空；junction 被索引跟随 | 真盘探测 + junction 测试 | `7e4424c` |
| W4 | 构建脚本 `python/` 与启动器 `runtime/` 不一致；embed 无 pip 仍调用 pip | 构建失败注入 | `af1a5d2` |
| W5 | 启动日志写包内，破坏指纹 | 运行前后对比 | `af1a5d2` |

## 原始命令（摘录）

```powershell
cd 归档盒源码
$env:QT_QPA_PLATFORM='offscreen'
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_product_backup*.py' -v
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_product_volume.py' -v
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_product_packaging.py' -v

powershell -File 打包/build_windows.ps1 `
  -PythonExe .venv\Scripts\python.exe `
  -GoExe 打包\_tools\go\bin\go.exe `
  -EmbedPythonZip 打包\_tools\python-3.14.0-embed-amd64.zip
```

## 未测边界

- 用户指定外置盘只读资料与安全弹出/重连
- 干净新用户账户、代码签名、高 DPI 手工点击
- Mac 端共享代码复验与 DMG（Task D）
