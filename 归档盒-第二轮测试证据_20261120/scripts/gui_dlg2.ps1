$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,$cond)
for($i=0;$i -lt $wins.Count;$i++){
  $x=$wins.Item($i)
  try {
    $name=$x.Current.Name; $pid2=$x.Current.ProcessId
    $edits = $x.FindAll([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Edit)))
    $hasFolder = $false
    foreach($e in $edits){ try { if($e.Current.Name -like "*文件夹*"){ $hasFolder=$true } } catch {} }
    if($name -like "*选择*" -or $hasFolder){
      "CAND win[{0}] name={1} pid={2} rect={3} edits={4}" -f $i,$name,$pid2,(Safe-Rect $x.Current.BoundingRectangle),$edits.Count
      foreach($e in $edits){ try { "   EDIT name=$($e.Current.Name) id=$($e.Current.AutomationId) rect=$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
      foreach($b in $x.FindAll([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)))){ try { if($b.Current.Name -like "*选择*"){ "   BTN name=$($b.Current.Name) rect=$(Safe-Rect $b.Current.BoundingRectangle)" } } catch {} }
    }
  } catch {}
}
