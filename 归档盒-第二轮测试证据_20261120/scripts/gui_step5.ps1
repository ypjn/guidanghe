$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
$nameCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,"选择文件夹")
$panes = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,$nameCond)
"matches=$($panes.Count)"
$confirm = $null
foreach($e in $panes){
  try {
    $r=$e.Current.BoundingRectangle
    "cand type=$($e.Current.ControlType.ProgrammaticName) rect=$(Safe-Rect $r)"
    if($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Pane'){ $confirm=$e }
  } catch {}
}
if($null -eq $confirm){ "confirm not found"; exit 1 }
Click-Element $confirm
Start-Sleep -Seconds 3
Shot (Join-Path $base "shots\G06-已选择文件夹.png")
$dlgCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,"选择要扫描的文件夹")
$still = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$dlgCond)
"dialog_open=$($null -ne $still)"
foreach($e in (Find-All-Type $w "Text")){
  try { if($e.Current.Name -and $e.Current.Name.Length -gt 3){ "TXT|$($e.Current.Name)" } } catch {}
}
Dump-Uia $w 300 | Out-File -Encoding utf8 (Join-Path $base "evidence\G06-uia.txt")
