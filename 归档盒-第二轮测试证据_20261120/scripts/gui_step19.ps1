$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 300
$r = Find-NameType $w "最近打开" "Button"
$r.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
Start-Sleep -Seconds 2
$rootEl = [System.Windows.Automation.AutomationElement]::RootElement
$dlg = $rootEl.FindFirst([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,"最近整理空间")))
if($null -eq $dlg){ "recent dialog not found"; exit 1 }
$items = Find-All-Type $dlg "DataItem"
"items=$($items.Count)"
if($items.Count -gt 0){
  $r0 = $items.Item(0).Current.BoundingRectangle
  "first_item=$(Safe-Rect $r0)"
}
# 点击第一列（单选列）中心
Click-XY 615 567
Start-Sleep -Milliseconds 800
$open = Find-NameType $dlg "打开空间" "Button"
"open_enabled=$($open.Current.IsEnabled)"
if($open.Current.IsEnabled){ $open.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Seconds 2 }
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "已打开|尚未扫描|本次范围|共扫描|扫描纳入")){ "TXT|$n" } } catch {} }
Shot (Join-Path $base "shots\G33-打开最近空间.png")
$s = Find-NameType $w "重新扫描" "Button"
"scan_enabled=$($s.Current.IsEnabled)"
if($s.Current.IsEnabled){ $s.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Seconds 6 }
$cfg = Find-NameType $w "学期和目录规则" "Button"
"cfg_enabled=$($cfg.Current.IsEnabled)"
if($cfg.Current.IsEnabled){
  $cfg.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
  Start-Sleep -Seconds 2
  Shot (Join-Path $base "shots\G34-分类配置.png")
  foreach($e in (Find-All-Type $w "TabItem")){ try { "TAB|$($e.Current.Name)" } catch {} }
  foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and $n.Length -gt 2 -and $n.Length -lt 40){ "CTXT|$n" } } catch {} }
  Dump-Uia $w 600 | Out-File -Encoding utf8 (Join-Path $base "evidence\G34-cfg-uia.txt")
}
