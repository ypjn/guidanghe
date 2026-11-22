# 在原生 Windows 上组装免安装包（嵌入式 Python + app 源码 + Go 启动器）。
# 不要在本脚本里写死某台电脑的路径；用参数或环境变量传入工具。
#
# 示例：
#   powershell -ExecutionPolicy Bypass -File 打包/build_windows.ps1 `
#     -PythonExe "C:\Python314\python.exe" `
#     -GoExe "C:\Program Files\Go\bin\go.exe"
#
# 产物：
#   打包/dist/归档盒-windows/
#   打包/dist/归档盒-<版本>-windows-x64.zip
#   打包/dist/SHA256SUMS-windows.txt

[CmdletBinding()]
param(
    [string]$PythonExe = $env:ARCHIVEBOX_BUILD_PYTHON,
    [string]$GoExe = $env:ARCHIVEBOX_BUILD_GO,
    [string]$EmbedPythonZip = $env:ARCHIVEBOX_EMBED_PYTHON_ZIP
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Pack = Join-Path $Root "打包"
$Src = Join-Path $Root "归档盒源码"
$Dist = Join-Path $Pack "dist"
$OutDir = Join-Path $Dist "归档盒-windows"

if (-not $PythonExe) {
    throw "请用 -PythonExe 或 ARCHIVEBOX_BUILD_PYTHON 指定已安装依赖的 Python。"
}
if (-not (Test-Path $PythonExe)) {
    throw "找不到 Python：$PythonExe"
}

Write-Host "使用 Python：$PythonExe"
& $PythonExe -c "import PySide6, pypdf; print('deps-ok')"

$Commit = (git -C $Root rev-parse HEAD 2>$null)
if (-not $Commit) { $Commit = "unknown" }
$Version = & $PythonExe -c "import sys; sys.path.insert(0, r'$Src'); from app_metadata import APP_VERSION; print(APP_VERSION)"

if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
New-Item -ItemType Directory -Path $OutDir | Out-Null
$AppDir = Join-Path $OutDir "app"
New-Item -ItemType Directory -Path $AppDir | Out-Null

# 复制应用源码（不含 tests / __pycache__）
Get-ChildItem $Src -File | Where-Object { $_.Extension -in ".py", ".txt", ".png" } | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $AppDir $_.Name)
}
Get-ChildItem $Src -Directory | Where-Object { $_.Name -notin @("tests", "__pycache__") } | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $AppDir $_.Name) -Recurse -Force
}

# 写入构建身份
$Identity = Join-Path $AppDir "build_identity.json"
& $PythonExe -c "import sys; sys.path.insert(0, r'$Src'); from app_metadata import write_build_identity; write_build_identity(r'$Identity', r'$Commit', 'windows-x64')"

# 安装运行依赖到包内 python（若提供嵌入式解释器 zip）
$Req = Join-Path $Pack "requirements-windows.txt"
if (-not (Test-Path $Req)) {
    throw "缺少 $Req"
}
if ($EmbedPythonZip -and (Test-Path $EmbedPythonZip)) {
    $PyDir = Join-Path $OutDir "python"
    New-Item -ItemType Directory -Path $PyDir | Out-Null
    Expand-Archive -Path $EmbedPythonZip -DestinationPath $PyDir -Force
    $EmbedPy = Get-ChildItem $PyDir -Recurse -Filter python.exe | Select-Object -First 1
    if (-not $EmbedPy) { throw "嵌入式 Python zip 中没有 python.exe" }
    & $EmbedPy.FullName -m pip install --upgrade pip
    & $EmbedPy.FullName -m pip install -r $Req
} else {
    Write-Host "未提供 ARCHIVEBOX_EMBED_PYTHON_ZIP；包内只含 app 源码，运行需本机 Python。"
    Copy-Item $Req (Join-Path $OutDir "requirements-windows.txt")
}

# 可选：编译 Go 启动器
$LauncherDir = Join-Path $Pack "win-launcher"
if ($GoExe -and (Test-Path $GoExe) -and (Test-Path (Join-Path $LauncherDir "main.go"))) {
    Write-Host "编译启动器：$GoExe"
    Push-Location $LauncherDir
    try {
        & $GoExe build -o (Join-Path $OutDir "归档盒.exe") .
    } finally {
        Pop-Location
    }
} else {
    Write-Host "跳过 Go 启动器（未提供 -GoExe 或源码缺失）。可用包内 Python 启动 app\main.py。"
    @"
@echo off
setlocal
set ROOT=%~dp0
set PYTHONPATH=%ROOT%app
python "%ROOT%app\main.py" %*
"@ | Set-Content -Encoding ASCII (Join-Path $OutDir "启动归档盒.bat")
}

$ZipName = "归档盒-$Version-windows-x64.zip"
$ZipPath = Join-Path $Dist $ZipName
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path $OutDir -DestinationPath $ZipPath
Get-FileHash -Algorithm SHA256 $ZipPath |
    ForEach-Object { "$($_.Hash.ToLower())  $ZipName" } |
    Set-Content -Encoding ASCII (Join-Path $Dist "SHA256SUMS-windows.txt")

Write-Host "已生成：$OutDir"
Write-Host "压缩包：$ZipPath"
Write-Host "构建提交：$Commit"
Write-Host "版本：$Version"
