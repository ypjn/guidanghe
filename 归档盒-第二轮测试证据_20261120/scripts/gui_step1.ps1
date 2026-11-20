$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
$app = Join-Path $base "app"
$py = (Get-ChildItem "C:\Users\Lenovo\Documents\Trae-CN\temp_20261119_02\test_env\*\*\runtime\python.exe" | Select-Object -First 1).FullName
$env:ARCHIVEBOX_APP_DIR = Join-Path $base "config\gui_app"
$env:ARCHIVEBOX_SCHEME_DIR = Join-Path $base "config\gui_schemes"
$env:ARCHIVEBOX_RECENT_FILE = Join-Path $base "config\gui_recent.json"
$log = Join-Path $base "logs\gui1_stdout.txt"
$err = Join-Path $base "logs\gui1_stderr.txt"
$p = Start-Process -FilePath $py -ArgumentList "`"$app\main.py`"" -WorkingDirectory $app -RedirectStandardOutput $log -RedirectStandardError $err -PassThru
"pid=$($p.Id)"
Start-Sleep -Seconds 6
. (Join-Path $base "gui_lib.ps1")
$w = Get-AppWindow
if ($null -eq $w) { "window not found"; Get-Content $err -Tail 60; exit 1 }
"window=$($w.Current.Name) rect=$($w.Current.BoundingRectangle)"
Shot (Join-Path $base "shots\G01-启动.png")
Dump-Uia $w 400 | Out-File -Encoding utf8 (Join-Path $base "evidence\G01-uia.txt")
"PID=$($p.Id)" | Out-File -Encoding utf8 (Join-Path $base "evidence\gui_pid.txt")
