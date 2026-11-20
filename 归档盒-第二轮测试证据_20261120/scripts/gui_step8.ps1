$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
$btn = Find-NameType $w "重新生成预览" "Button"
"regen_enabled=$($btn.Current.IsEnabled)"
Click-Element $btn
Start-Sleep -Seconds 4
Shot (Join-Path $base "shots\G11-按类型预览.png")
"----texts----"
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and $n.Length -gt 3){ "TXT|$n" } } catch {} }
"----rows----"
$rows = Find-All-Type $w "DataItem"
"rowcells=$($rows.Count)"
foreach($e in $rows){ try { "ROW|$($e.Current.Name)" } catch {} }
"----switch filter----"
$cb = Find-NameType $w "将改名" "CheckBox"
Click-Element $cb
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G12-将改名筛选.png")
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "计划|可见|已勾选|隐藏|将移动|跳过")){ "TXT|$n" } } catch {} }
$rows2 = Find-All-Type $w "DataItem"
"rowcells_filtered=$($rows2.Count)"
foreach($e in $rows2){ try { "FROW|$($e.Current.Name)" } catch {} }
Dump-Uia $w 400 | Out-File -Encoding utf8 (Join-Path $base "evidence\G12-uia.txt")
