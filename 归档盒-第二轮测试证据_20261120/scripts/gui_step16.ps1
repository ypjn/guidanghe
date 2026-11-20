$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
$app = Join-Path $base "app"
$py = (Get-ChildItem "C:\Users\Lenovo\Documents\Trae-CN\temp_20261119_02\test_env\*\*\runtime\python.exe" | Select-Object -First 1).FullName
$env:ARCHIVEBOX_APP_DIR = Join-Path $base "config\gui_app"
$env:ARCHIVEBOX_SCHEME_DIR = Join-Path $base "config\gui_schemes"
$env:ARCHIVEBOX_RECENT_FILE = Join-Path $base "config\gui_recent.json"
Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$p = Start-Process -FilePath $py -ArgumentList "`"$app\main.py`"" -WorkingDirectory $app -RedirectStandardOutput (Join-Path $base "logs\gui3_stdout.txt") -RedirectStandardError (Join-Path $base "logs\gui3_stderr.txt") -PassThru
"pid=$($p.Id)"
$proc = $null
for($i=0;$i -lt 50;$i++){
  Start-Sleep -Milliseconds 500
  $proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
  if($proc -and $proc.MainWindowHandle -ne 0){ break }
}
if(-not $proc -or $proc.MainWindowHandle -eq 0){ "window not ready"; Get-Content (Join-Path $base "logs\gui3_stderr.txt") -Tail 30; exit 1 }
. (Join-Path $base "gui_lib.ps1")
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 800
Shot (Join-Path $base "shots\G25-重启.png")
# 最近打开
$r = Find-NameType $w "最近打开" "Button"
Click-Element $r
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G26-最近打开单选.png")
$radios = Find-All-Type $w "RadioButton"
"radios=$($radios.Count)"
foreach($e in $radios){ try { "RADIO|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
$pick = $null
foreach($e in $radios){ try { if($e.Current.Name -like "*选择空间*"){ $pick=$e; break } } catch {} }
if($pick){
  Click-Element $pick
  Start-Sleep -Milliseconds 500
  $open = Find-NameType $w "打开空间" "Button"
  "open_enabled=$($open.Current.IsEnabled)"
  if($open.Current.IsEnabled){ Click-Element $open; Start-Sleep -Seconds 2 }
}
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "已打开|尚未扫描|本次范围|共扫描")){ "TXT|$n" } } catch {} }
Shot (Join-Path $base "shots\G27-最近打开后.png")
