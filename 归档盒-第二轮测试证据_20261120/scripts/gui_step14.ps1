$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
function Get-Dlg($root){
  $c = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)
  $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$c)
  foreach($x in $wins){ try { if($x.Current.Name -and $x.Current.Name -ne "归档盒 V1.7"){ return $x } } catch {} }
  return $null
}
function Close-Dlg($root){
  foreach($n in @("关闭","取消","确定","完成")){
    $b = Find-NameType $root $n "Button"
    if($b){ Click-Element $b; Start-Sleep -Milliseconds 800; return $n }
  }
  [System.Windows.Forms.SendKeys]::SendWait("{ESC}"); Start-Sleep -Milliseconds 800; return "ESC"
}
# 分类配置
$c = Find-NameType $w "学期和目录规则" "Button"
Click-Element $c
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G22-分类配置.png")
$d = Get-Dlg $w
if($d){ "CFG_DLG|$($d.Current.Name)|$(Safe-Rect $d.Current.BoundingRectangle)"; Dump-Uia $d 500 | Out-File -Encoding utf8 (Join-Path $base "evidence\G22-cfg-uia.txt") }
foreach($e in (Find-All-Type $w "TabItem")){ try { "TAB|$($e.Current.Name)" } catch {} }
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and $n.Length -gt 3 -and $n.Length -lt 60){ "CTXT|$n" } } catch {} }
"closed_by=$(Close-Dlg $w)"
Start-Sleep -Milliseconds 600
# 待整理清单
$t = Find-NameType $w "待整理清单" "Button"
Click-Element $t
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G23-待整理清单.png")
$d = Get-Dlg $w
if($d){ "INTAKE_DLG|$($d.Current.Name)|$(Safe-Rect $d.Current.BoundingRectangle)"; Dump-Uia $d 300 | Out-File -Encoding utf8 (Join-Path $base "evidence\G23-intake-uia.txt") }
"closed_by=$(Close-Dlg $w)"
Start-Sleep -Milliseconds 600
# 帮助菜单
$m = Find-NameType $w "帮助" "MenuItem"
if($m){ $m.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Milliseconds 800 }
foreach($e in (Find-All-Type $w "MenuItem")){ try { "MENU|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
Shot (Join-Path $base "shots\G24-帮助菜单.png")
