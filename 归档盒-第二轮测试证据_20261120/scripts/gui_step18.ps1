$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
function Invoke-El($el){ $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Milliseconds 800 }
$r = Find-NameType $w "最近打开" "Button"
"recent_enabled=$($r.Current.IsEnabled) off=$($r.Current.IsOffscreen) rect=$(Safe-Rect $r.Current.BoundingRectangle)"
Invoke-El $r
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G31-最近打开-invoke.png")
$rootEl = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,"最近整理空间")
$dlg = $rootEl.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
"dialog_found=$($null -ne $dlg)"
if($dlg){
  "DLG|$($dlg.Current.Name)|$(Safe-Rect $dlg.Current.BoundingRectangle)"
  Dump-Uia $dlg 400 | Out-File -Encoding utf8 (Join-Path $base "evidence\G31-recent-uia.txt")
  foreach($e in (Find-All-Type $dlg "RadioButton")){ try { "RADIO|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
  foreach($e in (Find-All-Type $dlg "Button")){ try { "BTN|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)|en=$($e.Current.IsEnabled)" } catch {} }
  $close = Find-NameType $dlg "关闭" "Button"
  if($close){ Invoke-El $close }
}
Start-Sleep -Milliseconds 600
$cfg = Find-NameType $w "学期和目录规则" "Button"
"cfg_enabled=$($cfg.Current.IsEnabled)"
Invoke-El $cfg
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G32-分类配置-invoke.png")
$cfgCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)
foreach($x in $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,$cfgCond)){ try { if($x.Current.Name -ne "归档盒 V1.7"){ "WIN|$($x.Current.Name)|$(Safe-Rect $x.Current.BoundingRectangle)" } } catch {} }
foreach($e in (Find-All-Type $w "TabItem")){ try { "TAB|$($e.Current.Name)" } catch {} }
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and $n.Length -gt 2 -and $n.Length -lt 40){ "CTXT|$n" } } catch {} }
Dump-Uia $w 600 | Out-File -Encoding utf8 (Join-Path $base "evidence\G32-cfg-uia.txt")
