$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
$u0 = Find-NameType $w "撤销上一次" "Button"
"undo_before_rescan=$($u0.Current.IsEnabled)"
$s = Find-NameType $w "重新扫描" "Button"
"scan_enabled=$($s.Current.IsEnabled)"
Click-Element $s
Start-Sleep -Seconds 6
$u1 = Find-NameType $w "撤销上一次" "Button"
"undo_after_rescan=$($u1.Current.IsEnabled)"
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "扫描纳入|计划|勾选|撤销")){ "TXT|$n" } } catch {} }
Shot (Join-Path $base "shots\G17-整理后重扫.png")
if($u1.Current.IsEnabled){
  Click-Element $u1
  Start-Sleep -Seconds 2
  Shot (Join-Path $base "shots\G18-撤销确认.png")
  foreach($e in (Find-All-Type $w "Button")){ try { "BTN|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
  foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "撤销|恢复|批次|确认")){ "TXT|$n" } } catch {} }
  Dump-Uia $w 400 | Out-File -Encoding utf8 (Join-Path $base "evidence\G18-uia.txt")
}
