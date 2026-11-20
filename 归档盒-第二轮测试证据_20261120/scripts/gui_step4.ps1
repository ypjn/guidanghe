$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
$cond = New-Object System.Windows.Automation.AndCondition(
  (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,"选择要扫描的文件夹")),
  (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window))
)
$dlg = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
if($null -eq $dlg){ "scan dialog not found"; exit 1 }
"dialog=$($dlg.Current.Name) hwnd=$($dlg.Current.NativeWindowHandle) rect=$(Safe-Rect $dlg.Current.BoundingRectangle)"
[UiNative]::SetForegroundWindow([IntPtr]$dlg.Current.NativeWindowHandle) | Out-Null
Start-Sleep -Milliseconds 400
[System.Windows.Forms.SendKeys]::SendWait("^l")
Start-Sleep -Milliseconds 500
Paste-Text (Join-Path $base "samples\课程 资料")
Start-Sleep -Milliseconds 800
[System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
Start-Sleep -Seconds 2
$still = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
if($still){
  "dialog still open after location enter"
  Dump-Uia $still 300 | Out-File -Encoding utf8 (Join-Path $base "evidence\G05-dialog-after-path.txt")
  [UiNative]::SetForegroundWindow([IntPtr]$still.Current.NativeWindowHandle) | Out-Null
  [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
  Start-Sleep -Seconds 2
}
Shot (Join-Path $base "shots\G05-选择后.png")
$after = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
"dialog_open=$($null -ne $after)"
# 打印主窗口状态文字
foreach($e in (Find-All-Type $w "Text")){
  try { if($e.Current.Name -and $e.Current.Name.Length -gt 4){ "TXT|$($e.Current.Name)" } } catch {}
}
