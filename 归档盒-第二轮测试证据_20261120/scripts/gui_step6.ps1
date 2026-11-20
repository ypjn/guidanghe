$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 500
$btn = Find-NameType $w "重新扫描" "Button"
"scan_button_enabled=$($btn.Current.IsEnabled)"
Click-Element $btn
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G07-扫描中.png")
# 处理可能出现的提示框（Qt 对话框）
$deadline = (Get-Date).AddSeconds(90)
$done = $false
while((Get-Date) -lt $deadline){
  $texts = @()
  foreach($e in (Find-All-Type $w "Text")){ try { $texts += $e.Current.Name } catch {} }
  $joined = $texts -join " / "
  if($joined -match "预览|将整理|共扫描|扫描完成"){ $done=$true; break }
  Start-Sleep -Milliseconds 800
}
"done=$done"
foreach($e in (Find-All-Type $w "Text")){
  try { if($e.Current.Name -and $e.Current.Name.Length -gt 3){ "TXT|$($e.Current.Name)" } } catch {}
}
# 所有对话框类窗口
$wins = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Window)))
foreach($x in $wins){ try { "WIN|$($x.Current.Name)|$(Safe-Rect $x.Current.BoundingRectangle)" } catch {} }
Shot (Join-Path $base "shots\G08-扫描完成.png")
Dump-Uia $w 500 | Out-File -Encoding utf8 (Join-Path $base "evidence\G08-uia.txt")
