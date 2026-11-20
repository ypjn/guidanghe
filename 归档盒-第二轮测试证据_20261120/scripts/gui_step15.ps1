$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
$side = $null
foreach($c in (Find-All-Type $w "ComboBox")){ try { $r=$c.Current.BoundingRectangle; if($r.X -lt 700 -and $r.Width -lt 400){ $side=$c } } catch {} }
Click-Element $side
Start-Sleep -Milliseconds 700
$item = Find-NameType $w "按方向/学期/类别（推荐）" "ListItem"
Click-Element $item
Start-Sleep -Seconds 1
$regen = Find-NameType $w "重新生成预览" "Button"
Click-Element $regen
Start-Sleep -Seconds 5
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "计划|已勾选|将移动|失效")){ "TXT|$n" } } catch {} }
$cfg = Find-NameType $w "学期和目录规则" "Button"
"cfg_enabled=$($cfg.Current.IsEnabled) rect=$(Safe-Rect $cfg.Current.BoundingRectangle)"
if($cfg.Current.IsEnabled){
  Click-Element $cfg
  Start-Sleep -Seconds 2
  Shot (Join-Path $base "shots\G25-分类配置.png")
  $c = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)
  foreach($x in $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,$c)){ try { if($x.Current.Name -ne "归档盒 V1.7"){ "CFG_DLG|$($x.Current.Name)|$(Safe-Rect $x.Current.BoundingRectangle)" } } catch {} }
  foreach($e in (Find-All-Type $w "TabItem")){ try { "TAB|$($e.Current.Name)" } catch {} }
  Dump-Uia $w 500 | Out-File -Encoding utf8 (Join-Path $base "evidence\G25-cfg-uia.txt")
  foreach($n in @("关闭","取消","确定","恢复默认","保存")){
    $b = Find-NameType $w $n "Button"
    if($b){ "close_by=$n"; Click-Element $b; break }
  }
  Start-Sleep -Milliseconds 800
}
