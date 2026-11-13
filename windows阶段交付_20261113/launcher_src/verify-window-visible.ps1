<#
.SYNOPSIS
验证归档盒 Windows 免安装包启动后主窗口是否真的可见。

.DESCRIPTION
启动指定启动器（默认包根目录的 启动归档盒.exe），等待 runtime\pythonw.exe 子进程出现，
再用 user32!EnumWindows 枚举该进程的顶层窗口，检查是否存在「可见且标题含 归档盒」的窗口。

这是针对「双击后进程在、桌面没窗口」缺陷的回归自检：
- 用修复版 启动归档盒.exe 测试应得到 PASS；
- 用原 归档盒.exe 测试应得到 FAIL（窗口存在但 visible=False）。

退出码：
  0 = 检测到可见主窗口（PASS）
  1 = 超时仍未检测到可见主窗口（FAIL，会打印该进程所有顶层窗口供诊断）
  2 = 参数或包结构错误

.PARAMETER PackageDir
免安装包根目录（应包含 app\、runtime\ 两个子目录）。

.PARAMETER Launcher
启动器文件名或完整路径，默认 启动归档盒.exe。

.PARAMETER TimeoutSeconds
等待窗口出现的超时秒数，默认 20。

.PARAMETER KeepRunning
检测结束后不结束测试启动的进程（默认会结束；只结束本次测试新起的 pythonw，不会动用户已打开的窗口）。

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\verify-window-visible.ps1 -PackageDir 'D:\归档盒-Windows-免安装'

.EXAMPLE
# 用原启动器复现缺陷（预期 FAIL，退出码 1）
powershell -ExecutionPolicy Bypass -File .\verify-window-visible.ps1 -PackageDir 'D:\归档盒-Windows-免安装' -Launcher '归档盒.exe'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageDir,
    [string]$Launcher = '启动归档盒.exe',
    [int]$TimeoutSeconds = 20,
    [switch]$KeepRunning
)

$ErrorActionPreference = 'Stop'

function Fail([string]$message, [int]$code) {
    Write-Host "[FAIL] $message" -ForegroundColor Red
    exit $code
}

# ---------- 0. 路径与包结构检查 ----------
if (-not (Test-Path -LiteralPath $PackageDir -PathType Container)) {
    Fail "包目录不存在：$PackageDir" 2
}
$pkg = (Resolve-Path -LiteralPath $PackageDir).Path
$appPy = Join-Path $pkg 'app\main.py'
$pyw = Join-Path $pkg 'runtime\pythonw.exe'
if (-not (Test-Path -LiteralPath $appPy -PathType Leaf)) { Fail "缺少 app\main.py：$appPy" 2 }
if (-not (Test-Path -LiteralPath $pyw -PathType Leaf)) { Fail "缺少 runtime\pythonw.exe：$pyw" 2 }
$runtimeDir = Join-Path $pkg 'runtime'

$launcherPath = $Launcher
if (-not [System.IO.Path]::IsPathRooted($launcherPath)) {
    $launcherPath = Join-Path $pkg $Launcher
}
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    Fail "启动器不存在：$launcherPath" 2
}

# ---------- 1. user32 窗口枚举 ----------
$probeSource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace ArchiveBoxVerify
{
    public class WindowProbe
    {
        public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);

        public static string[] Describe(uint targetPid)
        {
            var list = new List<string>();
            EnumWindows((hWnd, lParam) =>
            {
                uint pid;
                GetWindowThreadProcessId(hWnd, out pid);
                if (pid == targetPid)
                {
                    var title = new StringBuilder(512);
                    var cls = new StringBuilder(256);
                    GetWindowText(hWnd, title, 512);
                    GetClassName(hWnd, cls, 256);
                    list.Add("hwnd=0x" + hWnd.ToInt64().ToString("X") +
                             " visible=" + IsWindowVisible(hWnd) +
                             " class=[" + cls + "] title=[" + title + "]");
                }
                return true;
            }, IntPtr.Zero);
            return list.ToArray();
        }
    }
}
'@
if (-not ('ArchiveBoxVerify.WindowProbe' -as [type])) {
    Add-Type -TypeDefinition $probeSource -Language CSharp
}

# ---------- 2. 启动 ----------
$existingIds = @(Get-Process -Name pythonw -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$startedAt = Get-Date
Write-Host "包目录：$pkg"
Write-Host "启动器：$launcherPath"

try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $launcherPath
    $psi.WorkingDirectory = $pkg
    $psi.UseShellExecute = $false
    $launcher = [System.Diagnostics.Process]::Start($psi)
} catch {
    Fail "启动器启动失败：$($_.Exception.Message)" 1
}

# ---------- 3. 轮询窗口 ----------
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$visibleHit = $null
$hitProcess = $null
$lastReport = @()

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300
    $candidates = @(Get-Process -Name pythonw -ErrorAction SilentlyContinue | Where-Object {
        $procPath = $null
        try { $procPath = $_.Path } catch { }
        if (-not $procPath) { return $false }
        if ($existingIds -contains $_.Id) { return $false }
        return ($procPath.StartsWith($runtimeDir, [System.StringComparison]::OrdinalIgnoreCase))
    })
    foreach ($p in $candidates) {
        $descriptions = [ArchiveBoxVerify.WindowProbe]::Describe([uint32]$p.Id)
        if ($descriptions.Count -gt 0) { $lastReport = $descriptions }
        foreach ($d in $descriptions) {
            if ($d -match 'visible=True' -and $d -match '归档盒') {
                $visibleHit = $d
                $hitProcess = $p
                break
            }
        }
        if ($visibleHit) { break }
    }
    if ($visibleHit) { break }
}

$elapsed = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)

# ---------- 4. 结果 ----------
$testProcesses = @(Get-Process -Name pythonw -ErrorAction SilentlyContinue | Where-Object {
    if ($existingIds -contains $_.Id) { return $false }
    $procPath = $null
    try { $procPath = $_.Path } catch { }
    return ($procPath -and $procPath.StartsWith($runtimeDir, [System.StringComparison]::OrdinalIgnoreCase))
})

if ($visibleHit) {
    Write-Host "[PASS] ${elapsed}s 内检测到可见主窗口。" -ForegroundColor Green
    Write-Host "       $visibleHit"
    if ($hitProcess) {
        Write-Host ("       pythonw PID=" + $hitProcess.Id +
                    " 工作集=" + [math]::Round($hitProcess.WorkingSet64 / 1MB, 1) + " MB")
    }
} else {
    Write-Host "[FAIL] ${elapsed}s 内没有检测到可见的「归档盒」窗口。" -ForegroundColor Red
    if ($testProcesses.Count -eq 0) {
        Write-Host '       pythonw.exe 没有起来：启动器可能直接失败，请看包根目录「启动日志.txt」。'
    } elseif ($lastReport.Count -gt 0) {
        Write-Host '       该进程的顶层窗口（visible=False 即被隐藏）：'
        foreach ($d in $lastReport) { Write-Host "       $d" }
    } else {
        Write-Host '       pythonw.exe 在运行，但没有任何顶层窗口（可能卡在启动阶段）。'
    }
}

# ---------- 5. 清理本次测试启动的进程 ----------
if (-not $KeepRunning) {
    foreach ($p in $testProcesses) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch { }
    }
    if ($launcher -and -not $launcher.HasExited) {
        try { Stop-Process -Id $launcher.Id -Force -ErrorAction Stop } catch { }
    }
    Write-Host '已结束本次测试启动的进程。'
} else {
    Write-Host "已保留运行中的进程（PID: $((($testProcesses | ForEach-Object { $_.Id }) -join ', '))）。"
}

if ($visibleHit) { exit 0 } else { exit 1 }
