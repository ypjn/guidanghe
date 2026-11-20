$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
"----all combos----"
$combos = Find-All-Type $w "ComboBox"
foreach($c in $combos){ try { "COMBO|$($c.Current.Name)|$($c.Current.IsEnabled)|$(Safe-Rect $c.Current.BoundingRectangle)" } catch {} }
# 侧栏模式下拉：x 最小且在左侧
$side = $null
foreach($c in $combos){ try { $r=$c.Current.BoundingRectangle; if($r.X -lt 700 -and $r.Width -lt 400){ $side=$c } } catch {} }
if($null -eq $side){ "side combo not found"; exit 1 }
"side_combo=$($side.Current.Name) rect=$(Safe-Rect $side.Current.BoundingRectangle)"
Click-Element $side
Start-Sleep -Milliseconds 800
Shot (Join-Path $base "shots\G09-模式下拉.png")
$items = Find-All-Type $w "ListItem"
foreach($it in $items){ try { if($it.Current.BoundingRectangle.Y -gt 600 -and $it.Current.BoundingRectangle.X -lt 900){ "ITEM|$($it.Current.Name)|$(Safe-Rect $it.Current.BoundingRectangle)" } } catch {} }
$target = Find-NameType $w "按类型" "ListItem"
if($null -eq $target){ $target = Find-Any $w "按类型" }
if($null -eq $target){ "mode item not found"; exit 1 }
"clicking=$($target.Current.Name)"
Click-Element $target
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G10-切到按类型.png")
"----after mode switch----"
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "预览|勾选|计划|扫描|模式|请点击|未分类")){ "TXT|$n" } } catch {} }
$rows = Find-All-Type $w "DataItem"
"rows_after_switch=$($rows.Count)"
foreach($e in $rows){ try { "ROW|$($e.Current.Name)" } catch {} }
