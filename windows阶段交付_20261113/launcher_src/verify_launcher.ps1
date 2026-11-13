# 归档盒 P0 验证包 —— 启动器可见窗口验证（2026-11-13）
# 用法：powershell -ExecutionPolicy Bypass -File verify_launcher.ps1 -PackageDir "<包>"
param(
    [Parameter(Mandatory = $true)][string]$PackageDir,
    [int]$TimeoutSeconds = 60
)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class WinProbe {
    delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc cb, IntPtr p);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
    public static string[] WindowsOf(uint target) {
        var list = new List<string>();
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (pid == target) {
                var t = new StringBuilder(512); GetWindowTextW(h, t, 512);
                var c = new StringBuilder(256); GetClassNameW(h, c, 256);
                list.Add(h.ToString() + "|" + IsWindowVisible(h).ToString() + "|" + c.ToString() + "|" + t.ToString());
            }
            return true;
        }, IntPtr.Zero);
        return list.ToArray();
    }
}
"@

$PackageDir = (Resolve-Path -LiteralPath $PackageDir).Path.TrimEnd('\')
$exe = Join-Path $PackageDir '启动归档盒.exe'
$log = Join-Path $PackageDir '启动日志.txt'
if (-not (Test-Path -LiteralPath $exe)) { throw "缺少启动器：$exe" }
if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log -Force }
if (Test-Path -LiteralPath "$PackageDir\writetest.txt") { Remove-Item -LiteralPath "$PackageDir\writetest.txt" -Force }

$before = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Select-Object -ExpandProperty ProcessId)
Start-Process -FilePath $exe -WorkingDirectory $PackageDir

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$childPid = $null
$probe = @()
while ((Get-Date) -lt $deadline) {
    if (-not $childPid) {
        $cand = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
            Where-Object {
                $before -notcontains $_.ProcessId -and
                $_.CommandLine -and $_.CommandLine -like "*$PackageDir*"
            })
        if ($cand.Count -gt 0) { $childPid = [int]$cand[0].ProcessId }
    }
    if ($childPid) {
        $probe = @([WinProbe]::WindowsOf([uint32]$childPid))
        $visible = @($probe | Where-Object { $_ -match '\|True\|' })
        if ($visible.Count -gt 0) { break }
    }
    Start-Sleep -Milliseconds 300
}

$elapsed = [math]::Round(($TimeoutSeconds - [math]::Max(0, ($deadline - (Get-Date)).TotalSeconds)), 1)
$visible = @($probe | Where-Object { $_ -match '\|True\|' })
$logText = if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Raw -Encoding UTF8 } else { '' }

Write-Output "包目录: $PackageDir"
Write-Output "pythonw PID: $childPid"
Write-Output "全部顶层窗口:"
$probe | ForEach-Object { Write-Output "  $_" }
Write-Output "启动日志.txt 内容:"
Write-Output ($logText -split "`r?`n" | ForEach-Object { "  $_" } | Out-String)

if ($visible.Count -gt 0) {
    Write-Output ("[PASS] 在 {0}s 内检测到可见主窗口" -f $elapsed)
    $code = 0
} else {
    Write-Output ("[FAIL] {0}s 内没有可见窗口（窗口对象若存在，上面会显示 visible=False）" -f $TimeoutSeconds)
    $code = 1
}

if ($childPid) {
    try { Stop-Process -Id $childPid -Force -ErrorAction Stop; Write-Output "已结束 pythonw PID=$childPid" } catch { Write-Output "结束进程失败: $($_.Exception.Message)" }
}
exit $code
