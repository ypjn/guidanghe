$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 500
$btn = Find-NameType $w "选择文件夹" "Button"
Click-Element $btn
Start-Sleep -Seconds 2
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,$cond)
"windows=$($wins.Count)"
for($i=0;$i -lt $wins.Count;$i++){
  $x=$wins.Item($i)
  try { "win[{0}] name={1} pid={2} rect={3}" -f $i,$x.Current.Name,$x.Current.ProcessId,(Safe-Rect $x.Current.BoundingRectangle) } catch {}
}
Shot (Join-Path $base "shots\G04-选择文件夹对话框.png")
$dlg = $null
for($i=0;$i -lt $wins.Count;$i++){
  $x=$wins.Item($i)
  try { if($x.Current.Name -like "*选择*文件夹*" -or $x.Current.Name -like "*文件夹*"){ $dlg=$x; break } } catch {}
}
if($null -eq $dlg){ "dialog not found"; exit 1 }
"dialog=$($dlg.Current.Name)"
Dump-Uia $dlg 400 | Out-File -Encoding utf8 (Join-Path $base "evidence\G04-dialog-uia.txt")
