$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
foreach($n in @("识别图片文字","待定或识别失败时问 DeepSeek","待定","将改名")){
  $cb = Find-NameType $w $n "CheckBox"
  if($cb){ $tp=$cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern); "TOGGLE|$n|$($tp.Current.ToggleState)" }
}
function Get-Dlg($root){
  $c = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)
  $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$c)
  foreach($x in $wins){ try { if($x.Current.Name -and $x.Current.Name -ne "归档盒 V1.7" -and $x.Current.Name -ne "选择要扫描的文件夹"){ return $x } } catch {} }
  return $null
}
function Close-Dlg($root){
  foreach($n in @("关闭","取消","确定","完成")){
    $b = Find-NameType $root $n "Button"
    if($b){ Click-Element $b; Start-Sleep -Milliseconds 800; return $n }
  }
  [System.Windows.Forms.SendKeys]::SendWait("{ESC}")
  Start-Sleep -Milliseconds 800
  return "ESC"
}
# 操作历史
$h = Find-NameType $w "操作历史" "Button"
Click-Element $h
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G19-操作历史.png")
$d = Get-Dlg $w
if($d){ "HISTORY_DLG|$($d.Current.Name)|$(Safe-Rect $d.Current.BoundingRectangle)"; Dump-Uia $d 500 | Out-File -Encoding utf8 (Join-Path $base "evidence\G19-history-uia.txt") }
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "批次|撤销|恢复|成功|移动")){ "HTXT|$n" } } catch {} }
"closed_by=$(Close-Dlg $w)"
Start-Sleep -Milliseconds 500
# 最近打开
$r = Find-NameType $w "最近打开" "Button"
Click-Element $r
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G20-最近打开.png")
$d = Get-Dlg $w
if($d){ "RECENT_DLG|$($d.Current.Name)|$(Safe-Rect $d.Current.BoundingRectangle)"; Dump-Uia $d 300 | Out-File -Encoding utf8 (Join-Path $base "evidence\G20-recent-uia.txt") }
foreach($e in (Find-All-Type $w "RadioButton")){ try { "RADIO|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
"closed_by=$(Close-Dlg $w)"
Start-Sleep -Milliseconds 500
# 查看重复文件
$dup = Find-NameType $w "查看重复文件" "Button"
"dup_enabled=$($dup.Current.IsEnabled)"
Click-Element $dup
Start-Sleep -Seconds 4
Shot (Join-Path $base "shots\G21-重复文件.png")
$d = Get-Dlg $w
if($d){ "DUP_DLG|$($d.Current.Name)|$(Safe-Rect $d.Current.BoundingRectangle)"; Dump-Uia $d 400 | Out-File -Encoding utf8 (Join-Path $base "evidence\G21-dup-uia.txt") }
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "重复|份|核对|同名")){ "DTXT|$n" } } catch {} }
"closed_by=$(Close-Dlg $w)"
