[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ZipPath,
    [Parameter(Mandatory = $true)][string]$EvidenceDir,
    [string]$ExtractRoot = ""
)

$ErrorActionPreference = "Stop"
$ZipPath = (Resolve-Path $ZipPath).Path
if (-not $ExtractRoot) {
    $ExtractRoot = Join-Path $env:TEMP ("ArchiveBox Accept " + [guid]::NewGuid().ToString("N").Substring(0, 8))
}
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
New-Item -ItemType Directory -Force -Path $ExtractRoot | Out-Null

Write-Host "extract: $ExtractRoot"
Expand-Archive -Path $ZipPath -DestinationPath $ExtractRoot -Force
$AppRoot = Get-ChildItem $ExtractRoot -Directory | Where-Object {
    Test-Path (Join-Path $_.FullName "app\main.py")
} | Select-Object -First 1
if (-not $AppRoot) { throw "app\main.py not found after extract" }
$Root = $AppRoot.FullName

$Report = [ordered]@{
    zip = $ZipPath
    extract_root = $ExtractRoot
    app_root = $Root
    checks = @()
}

function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $script:Report.checks += [ordered]@{ name = $Name; ok = $Ok; detail = $Detail }
    if (-not $Ok) { throw "FAIL: $Name -- $Detail" }
    Write-Host "[OK] $Name : $Detail"
}

$Exe = Join-Path $Root "归档盒.exe"
$RuntimePyw = Join-Path $Root "runtime\pythonw.exe"
$RuntimePy = Join-Path $Root "runtime\python.exe"
$Identity = Join-Path $Root "app\build_identity.json"
Add-Check "exe" (Test-Path $Exe) $Exe
Add-Check "runtime_pythonw" (Test-Path $RuntimePyw) $RuntimePyw
Add-Check "identity" (Test-Path $Identity) $Identity

$IdentityObj = Get-Content $Identity -Raw -Encoding UTF8 | ConvertFrom-Json
$Report.version = $IdentityObj.version
$Report.commit = $IdentityObj.commit
Add-Check "commit_not_unknown" ($IdentityObj.commit -and $IdentityObj.commit -ne "unknown") $IdentityObj.commit

$env:PYTHONPATH = $null
$env:ARCHIVEBOX_BUILD_PYTHON = $null

function Get-TreeFingerprint([string]$Path) {
    $files = Get-ChildItem $Path -Recurse -File | Sort-Object FullName
    $lines = foreach ($f in $files) {
        $rel = $f.FullName.Substring($Path.Length).TrimStart("\").Replace("\", "/")
        "{0}|{1}" -f $rel, $f.Length
    }
    $text = ($lines -join "`n")
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $h = [System.Security.Cryptography.SHA256]::Create()
    return ([BitConverter]::ToString($h.ComputeHash($bytes)) -replace "-", "").ToLower()
}

$Before = Get-TreeFingerprint $Root
$Report.fingerprint_before = $Before
$Report.file_count_before = (Get-ChildItem $Root -Recurse -File).Count

$SmokePy = @"
import json, os, sys, tempfile
root = r'''$Root'''
sys.path.insert(0, os.path.join(root, 'app'))
os.environ['ARCHIVEBOX_APP_DIR'] = tempfile.mkdtemp(prefix='archivebox_accept_')
os.environ['ARCHIVEBOX_LOG_DIR'] = os.environ['ARCHIVEBOX_APP_DIR']
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import archivebox_dllpath
import backup_dialog, backup_service, volume_identity, main
from app_metadata import APP_VERSION
print(json.dumps({'imports_ok': True, 'version': APP_VERSION}, ensure_ascii=False))
"@
$SmokeFile = Join-Path $EvidenceDir "smoke_imports.py"
Set-Content -Encoding UTF8 $SmokeFile $SmokePy
& $RuntimePy $SmokeFile
Add-Check "smoke_imports" ($LASTEXITCODE -eq 0) "exit=$LASTEXITCODE"

$AcceptWork = Join-Path $EvidenceDir "accept-workdir"
New-Item -ItemType Directory -Force -Path $AcceptWork | Out-Null
$Proc = Start-Process -FilePath $Exe `
    -ArgumentList @("--product-acceptance", "smoke-gui", $AcceptWork) `
    -WorkingDirectory (Join-Path $Root "app") -PassThru -Wait -NoNewWindow
Add-Check "smoke_gui_exit" ($Proc.ExitCode -eq 0) "exit=$($Proc.ExitCode)"

$After = Get-TreeFingerprint $Root
$Report.fingerprint_after = $After
Add-Check "package_unchanged" ($Before -eq $After) "before=$Before after=$After"

$Bad = @(Get-ChildItem $Root -Recurse -File | Where-Object {
    $_.Name -match "credentials|secrets|\.env$" -or
    $_.FullName -match "\\tests\\" -or
    $_.FullName -match "\\.venv\\"
})
Add-Check "no_private_or_tests" ($Bad.Count -eq 0) ("count=" + $Bad.Count)

$LogInPackage = Test-Path (Join-Path $Root "启动日志.txt")
Add-Check "no_log_in_package" (-not $LogInPackage) "startup_log_in_package=$LogInPackage"

$OutJson = Join-Path $EvidenceDir "accept_windows_report.json"
($Report | ConvertTo-Json -Depth 6) | Set-Content -Encoding UTF8 $OutJson
Write-Host "report=$OutJson"
Write-Host "ACCEPT_OK"
exit 0