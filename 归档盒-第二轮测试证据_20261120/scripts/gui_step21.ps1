$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 300
foreach($n in @("关闭","取消")){ $b=Find-NameType $w $n "Button"; if($b){ $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Milliseconds 900; break } }
$side = $null
foreach($c in (Find-All-Type $w "ComboBox")){ try { $r=$c.Current.BoundingRectangle; if($r.X -lt 700 -and $r.Width -lt 400){ $side=$c } } catch {} }
"combo=$(Safe-Rect $side.Current.BoundingRectangle)"
$side.SetFocus()
Start-Sleep -Milliseconds 300
[System.Windows.Forms.SendKeys]::SendWait("{DOWN}{DOWN}")
Start-Sleep -Milliseconds 800
Shot (Join-Path $base "shots\G36-按键选模式.png")
$regen = Find-NameType $w "重新生成预览" "Button"
$regen.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
Start-Sleep -Seconds 5
Shot (Join-Path $base "shots\G37-按课程模式.png")
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "计划|勾选|未分类|将移动")){ "TXT|$n" } } catch {} }
$rows = Find-All-Type $w "DataItem"
foreach($e in $rows){ try { $n=$e.Current.Name; if($n -match "作业|笔记|课堂"){ "ROW|$n" } } catch {} }
