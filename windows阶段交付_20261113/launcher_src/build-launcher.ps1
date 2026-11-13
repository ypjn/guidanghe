<#
.SYNOPSIS
编译「归档盒 Windows 免安装包」修复版启动器（启动归档盒.exe）。

.DESCRIPTION
使用 Windows 自带的 .NET Framework 4.x 编译器 csc.exe 编译 launcher_fix.cs。
Windows 10 / 11 默认自带 .NET Framework 4.x，无需安装 Go、Python 或 Visual Studio。

.PARAMETER Source
C# 源码路径，默认本脚本同目录的 launcher_fix.cs。

.PARAMETER Output
输出 exe 路径，默认本脚本同目录的 启动归档盒.exe。

.PARAMETER Icon
可选图标路径，默认本脚本同目录的 app_icon.ico（从原 归档盒.exe 提取，保证图标一致）。
若文件不存在则编译成默认图标，不报错。

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\build-launcher.ps1
#>
[CmdletBinding()]
param(
    [string]$Source = (Join-Path $PSScriptRoot 'launcher_fix.cs'),
    [string]$Output = (Join-Path $PSScriptRoot '启动归档盒.exe'),
    [string]$Icon = (Join-Path $PSScriptRoot 'app_icon.ico')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw "找不到源码：$Source"
}

$cscCandidates = @(
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
)
$csc = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $csc) {
    throw "找不到 csc.exe。请确认已启用 .NET Framework 4.x（Windows 10/11 默认自带）。"
}

$cscArgs = @(
    '/nologo',
    '/target:winexe',
    '/optimize+',
    "/out:$Output",
    '/r:System.Windows.Forms.dll',
    '/r:System.Drawing.dll'
)
if (Test-Path -LiteralPath $Icon -PathType Leaf) {
    $cscArgs += "/win32icon:$Icon"
}
$cscArgs += $Source

Write-Host "编译器：$csc"
Write-Host "源码：$Source"
Write-Host "输出：$Output"
& $csc @cscArgs
if ($LASTEXITCODE -ne 0) {
    throw "编译失败，csc 退出码 $LASTEXITCODE"
}

Write-Host ''
Write-Host '编译完成。' -ForegroundColor Green
Get-Item -LiteralPath $Output | Select-Object FullName, Length, LastWriteTime | Format-List
Get-FileHash -LiteralPath $Output -Algorithm SHA256 | Select-Object Algorithm, Hash | Format-List
