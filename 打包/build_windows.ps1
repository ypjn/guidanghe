# Assemble Windows portable package: runtime + app + ArchiveBox.exe
# Layout matches Go launcher: runtime\pythonw.exe , app\main.py

[CmdletBinding()]
param(
    [string]$PythonExe = $env:ARCHIVEBOX_BUILD_PYTHON,
    [string]$GoExe = $env:ARCHIVEBOX_BUILD_GO,
    [string]$EmbedPythonZip = $env:ARCHIVEBOX_EMBED_PYTHON_ZIP,
    [switch]$AllowUnknownCommit,
    [switch]$AllowSourceOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Pack = Join-Path $Root "打包"
$Src = Join-Path $Root "归档盒源码"
$Dist = Join-Path $Pack "dist"
$OutDir = Join-Path $Dist "归档盒-windows"
$Tools = Join-Path $Pack "_tools"

function Assert-ExitCode {
    param([string]$Label, [int]$Code)
    if ($Code -ne 0) {
        throw "$Label failed, exit=$Code"
    }
}

function Invoke-Native {
    param([string]$Label, [string]$FilePath, [Parameter(ValueFromRemainingArguments=$true)][string[]]$ArgumentList)
    Write-Host ">> $Label"
    & $FilePath @ArgumentList
    Assert-ExitCode $Label $LASTEXITCODE
}

if (-not $PythonExe) {
    $candidate = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $candidate) { $PythonExe = $candidate }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw "Pass -PythonExe or ARCHIVEBOX_BUILD_PYTHON"
}
if (-not $GoExe) {
    $candidate = Join-Path $Tools "go\bin\go.exe"
    if (Test-Path $candidate) { $GoExe = $candidate }
}
if (-not $EmbedPythonZip) {
    $candidate = Get-ChildItem $Tools -Filter "python-*-embed-amd64.zip" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($candidate) { $EmbedPythonZip = $candidate.FullName }
}

Write-Host "Python: $PythonExe"
Invoke-Native "check-deps" $PythonExe "-c" "import PySide6, pypdf; print('deps-ok')"

$Commit = (& git -C $Root rev-parse HEAD 2>$null)
if (-not $Commit) { $Commit = "unknown" }
if ($Commit -eq "unknown" -and -not $AllowUnknownCommit) {
    throw "Cannot resolve git commit; refuse release build (use -AllowUnknownCommit to override)"
}
$Version = & $PythonExe -c "import sys; sys.path.insert(0, r'$Src'); from app_metadata import APP_VERSION; print(APP_VERSION)"
Assert-ExitCode "read-version" $LASTEXITCODE
$Version = ($Version | Select-Object -Last 1).ToString().Trim()
if (-not $Version) { throw "empty version" }

if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
New-Item -ItemType Directory -Path $OutDir | Out-Null
$AppDir = Join-Path $OutDir "app"
New-Item -ItemType Directory -Path $AppDir | Out-Null

$SkipDirs = @("tests", "__pycache__", ".venv", ".git", ".archivebox-backup", ".organizer", "dist", "build", "evidence")
$SkipNames = @(".env", "credentials.json", "secrets.json", "Thumbs.db", ".DS_Store")
Get-ChildItem $Src -File | Where-Object {
    $_.Extension -in ".py", ".txt", ".png", ".json", ".md" -and $_.Name -notin $SkipNames
} | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $AppDir $_.Name) -Force
}
Get-ChildItem $Src -Directory | Where-Object { $_.Name -notin $SkipDirs } | ForEach-Object {
    $dest = Join-Path $AppDir $_.Name
    Copy-Item $_.FullName $dest -Recurse -Force
    Get-ChildItem $dest -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

$DllHelper = Join-Path $Pack "win-launcher\archivebox_dllpath.py"
if (Test-Path $DllHelper) {
    Copy-Item $DllHelper (Join-Path $AppDir "archivebox_dllpath.py") -Force
}

$Identity = Join-Path $AppDir "build_identity.json"
$IdentityPy = @"
import sys
sys.path.insert(0, r'$Src')
from app_metadata import write_build_identity
write_build_identity(r'$Identity', r'$Commit', 'windows-x64')
"@
$IdentityScript = Join-Path $Dist "_write_identity.py"
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
Set-Content -Path $IdentityScript -Value $IdentityPy -Encoding UTF8
Invoke-Native "write-identity" $PythonExe $IdentityScript
Remove-Item $IdentityScript -Force -ErrorAction SilentlyContinue

$Req = Join-Path $Pack "requirements-windows-portable.txt"
if (-not (Test-Path $Req)) { throw "missing $Req" }

if (-not $AllowSourceOnly) {
    if (-not $EmbedPythonZip -or -not (Test-Path $EmbedPythonZip)) {
        throw "missing embed python zip (-EmbedPythonZip)"
    }
    if (-not $GoExe -or -not (Test-Path $GoExe)) {
        throw "missing go (-GoExe); required for ArchiveBox.exe"
    }

    $RuntimeDir = Join-Path $OutDir "runtime"
    New-Item -ItemType Directory -Path $RuntimeDir | Out-Null
    Expand-Archive -Path $EmbedPythonZip -DestinationPath $RuntimeDir -Force
    $EmbedPy = Join-Path $RuntimeDir "python.exe"
    $EmbedPyw = Join-Path $RuntimeDir "pythonw.exe"
    if (-not (Test-Path $EmbedPy)) { throw "embed zip missing python.exe" }
    if (-not (Test-Path $EmbedPyw)) { throw "embed zip missing pythonw.exe" }

    $Pth = Get-ChildItem $RuntimeDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $Pth) { throw "embed zip missing python*._pth" }
    $zipName = $Pth.Name -replace "\._pth$", ".zip"
    @(
        $zipName
        "."
        "Lib\site-packages"
        "..\app"
        "import site"
    ) | Set-Content -Encoding ASCII $Pth.FullName

    $Site = Join-Path $RuntimeDir "Lib\site-packages"
    New-Item -ItemType Directory -Path $Site -Force | Out-Null
    Invoke-Native "pip-target-install" $PythonExe "-m" "pip" "install" "--upgrade" "--target" $Site "-r" $Req

    $QWindows = Get-ChildItem $Site -Recurse -Filter "qwindows.dll" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $QWindows) { throw "qwindows.dll missing after PySide6 install" }

    $LauncherDir = Join-Path $Pack "win-launcher"
    if (-not (Test-Path (Join-Path $LauncherDir "main.go"))) { throw "missing win-launcher/main.go" }
    Write-Host "build launcher: $GoExe"
    $env:CGO_ENABLED = "0"
    $env:GOTMPDIR = Join-Path $Tools "gotmp"
    $env:GOCACHE = Join-Path $Tools "gocache"
    $env:GOPATH = Join-Path $Tools "gopath"
    $env:GOMODCACHE = Join-Path $Tools "gomodcache"
    foreach ($d in @($env:GOTMPDIR, $env:GOCACHE, $env:GOPATH, $env:GOMODCACHE)) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
    Push-Location $LauncherDir
    try {
        & $GoExe build -o (Join-Path $OutDir "归档盒.exe") .
        Assert-ExitCode "go-build" $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if (-not (Test-Path (Join-Path $OutDir "归档盒.exe"))) { throw "归档盒.exe not produced" }

    @(
        "@echo off"
        "setlocal"
        "set ROOT=%~dp0"
        '"%ROOT%runtime\python.exe" "%ROOT%app\main.py" %*'
        "exit /b %ERRORLEVEL%"
    ) | Set-Content -Encoding ASCII (Join-Path $OutDir "启动归档盒.bat")
} else {
    Write-Host "AllowSourceOnly: source package without runtime"
    Copy-Item $Req (Join-Path $OutDir "requirements-windows-portable.txt")
    @(
        "@echo off"
        "setlocal"
        "set ROOT=%~dp0"
        "set PYTHONPATH=%ROOT%app"
        'python "%ROOT%app\main.py" %*'
        "exit /b %ERRORLEVEL%"
    ) | Set-Content -Encoding ASCII (Join-Path $OutDir "启动归档盒.bat")
}

$Readme = @"
ArchiveBox Windows portable
version: $Version
commit: $Commit

Double-click 归档盒.exe. Keep the whole folder.
Logs/data: %LOCALAPPDATA%\归档盒\
"@
Set-Content -Encoding UTF8 (Join-Path $OutDir "使用说明.txt") $Readme

$ZipName = "归档盒-$Version-windows-x64.zip"
$ZipPath = Join-Path $Dist $ZipName
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path $OutDir -DestinationPath $ZipPath
$hash = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash.ToLower()
"$hash  $ZipName" | Set-Content -Encoding ASCII (Join-Path $Dist "SHA256SUMS-windows.txt")

Write-Host "OUT=$OutDir"
Write-Host "ZIP=$ZipPath"
Write-Host "SHA256=$hash"
Write-Host "COMMIT=$Commit"
Write-Host "VERSION=$Version"
