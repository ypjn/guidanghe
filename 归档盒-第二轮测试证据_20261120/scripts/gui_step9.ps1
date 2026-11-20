$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
# 取消“将改名”筛选
$cb = Find-NameType $w "将改名" "CheckBox"
Click-Element $cb
Start-Sleep -Milliseconds 800
# 开始整理
$btn = Find-NameType $w "开始整理" "Button"
"execute_enabled=$($btn.Current.IsEnabled)"
Click-Element $btn
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G13-确认整理.png")
"----windows----"
$wins = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)))
foreach($x in $wins){ try { "WIN|$($x.Current.Name)|$(Safe-Rect $x.Current.BoundingRectangle)" } catch {} }
"----buttons----"
foreach($e in (Find-All-Type $w "Button")){ try { "BTN|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
"----texts----"
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and $n.Length -gt 3){ "TXT|$n" } } catch {} }
Dump-Uia $w 500 | Out-File -Encoding utf8 (Join-Path $base "evidence\G13-uia.txt")
