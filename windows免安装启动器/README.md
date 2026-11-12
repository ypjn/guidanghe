# 归档盒 Windows 免安装包 —— 修复版启动器

> 针对「双击 `归档盒.exe` 后进程在、桌面没有窗口」缺陷的修复件。
> 完整分析见 [`归档盒-项目设计/缺陷记录_20261112_Windows免安装启动器隐藏窗口.md`](../归档盒-项目设计/缺陷记录_20261112_Windows免安装启动器隐藏窗口.md)。

## 一分钟用法

1. 把本目录的 **`启动归档盒.exe`** 复制到免安装包根目录（和原 `归档盒.exe` 同级）。
2. 以后**双击 `启动归档盒.exe`** 启动。窗口约 1 秒内出现。
3. 原来的 `归档盒.exe` 保留不动即可（可以留着做对照，也可以删掉，不影响使用）。

首次运行若出现「Windows 已保护你的电脑」（无数字签名的正常提示）：
点「更多信息」→「仍要运行」。

## 它和原启动器有什么不同

| 项 | 原 `归档盒.exe` | `启动归档盒.exe` |
|---|---|---|
| 启动方式 | `runtime\pythonw.exe app\main.py` | 相同 |
| 工作目录 | `app\` | 相同 |
| `PYTHONUTF8=1` | 是 | 是 |
| 写「启动日志.txt」 | 是（空文件） | 是（含启动时间/路径） |
| 校验 `app\main.py`、`runtime\pythonw.exe` | 是 | 是 |
| **子进程窗口** | **隐藏（缺陷）** | **正常显示** |
| 数字签名 | 无 | 无 |

原启动器是 Go 程序，用隐藏窗口方式拉起 `pythonw.exe`，Windows 会把 Qt 主窗口的
第一次 `ShowWindow` 覆盖成隐藏 —— 所以程序在跑、窗口永远不出现。

## 文件清单

| 文件 | 用途 |
|---|---|
| `启动归档盒.exe` | 修复版启动器（预编译，可直接用） |
| `launcher_fix.cs` | 启动器 C# 源码（单文件，无第三方依赖） |
| `build-launcher.ps1` | 用 Windows 自带 `csc.exe` 重新编译 |
| `verify-window-visible.ps1` | 冒烟验证：启动后枚举窗口，检查主窗口是否真的可见 |
| `app_icon.ico` | 从原 `归档盒.exe` 提取的图标，仅用于保持图标一致 |

## 重新编译

```powershell
cd windows免安装启动器
powershell -ExecutionPolicy Bypass -File .\build-launcher.ps1
```

- 需要 Windows 自带 .NET Framework 4.x（Windows 10/11 默认自带，`csc.exe` 位于
  `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\`），**不需要**安装 Go / Python / Visual Studio。
- 输出会打印文件大小与 SHA-256。
- 源码文件请保持 UTF-8 with BOM（`csc.exe` 按 ANSI 读取无 BOM 源码时中文会乱码）。

## 冒烟验证

```powershell
# 用修复版启动器验证：预期 PASS（退出码 0）
powershell -ExecutionPolicy Bypass -File .\verify-window-visible.ps1 -PackageDir "<免安装包根目录>"

# 用原启动器复现缺陷：预期 FAIL（退出码 1，并打印 visible=False 的窗口）
powershell -ExecutionPolicy Bypass -File .\verify-window-visible.ps1 -PackageDir "<免安装包根目录>" -Launcher "归档盒.exe" -TimeoutSeconds 10
```

脚本结束时会关掉**本次测试新起**的 `pythonw.exe`，不会影响用户已打开的窗口。

## 校验和

| 文件 | 大小 | SHA-256 |
|---|---|---|
| `启动归档盒.exe` | 7,168 | `56A829CDE3DA0109B18E425B76DC8D4A28DFFAD2AAE8CE6633E314517FB3E61A` |

## 已知限制

- 只解决「启动后窗口不可见」问题，不改变归档盒本体的任何行为与数据。
- exe 没有数字签名，首次运行可能有 SmartScreen 提示。
- 仅适用于 Windows x64 免安装包（`runtime\pythonw.exe` + `app\main.py` 结构）。
- 如果打包端已修好 Go 启动器并重新出包，本修复件即可弃用；本目录仍可作为回归验证工具。
- 若后续在启动器源码里加参数透传（如 `--open <目录>`），两边需同步，避免行为分叉。
