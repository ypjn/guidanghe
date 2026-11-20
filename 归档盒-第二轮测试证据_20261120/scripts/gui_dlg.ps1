$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,"选择文件夹")
$dlg = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
if($null -eq $dlg){ "dialog not found by name"; exit 1 }
"dlg type=$($dlg.Current.ControlType.ProgrammaticName) pid=$($dlg.Current.ProcessId) rect=$($dlg.Current.BoundingRectangle)"
Dump-Uia $dlg 500 | Out-File -Encoding utf8 (Join-Path $base "evidence\G04-dialog-uia.txt")
$edits = Find-All-Type $dlg "Edit"
foreach($e in $edits){ try { "EDIT|$($e.Current.Name)|$($e.Current.AutomationId)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
$btns = Find-All-Type $dlg "Button"
foreach($e in $btns){ try { "BTN|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
