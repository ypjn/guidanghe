$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 300
$ok = Find-NameType $w "确定" "Button"
if($ok){ Click-Element $ok; Start-Sleep -Seconds 1 }
$u = Find-NameType $w "撤销上一次" "Button"
"undo_enabled=$($u.Current.IsEnabled)"
Click-Element $u
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G16-撤销确认.png")
foreach($e in (Find-All-Type $w "Button")){ try { "BTN|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "撤销|恢复|批次|确认")){ "TXT|$n" } } catch {} }
Dump-Uia $w 400 | Out-File -Encoding utf8 (Join-Path $base "evidence\G16-uia.txt")
