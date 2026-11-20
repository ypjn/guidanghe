$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
. (Join-Path $base "gui_lib.ps1")
$proc = Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*归档盒*" } | Select-Object -First 1
if (-not $proc) { "app process not found"; exit 1 }
$h = [IntPtr]$proc.MainWindowHandle
"pid=$($proc.Id) title=$($proc.MainWindowTitle) hwnd=$h"
$w = [System.Windows.Automation.AutomationElement]::FromHandle($h)
"rect=$($w.Current.BoundingRectangle)"
[UiNative]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 800
Shot (Join-Path $base "shots\G03-主窗口.png")
Dump-Uia $w 900 | Out-File -Encoding utf8 (Join-Path $base "evidence\G03-uia.txt")
$lines = @()
foreach($t in @('Button','CheckBox','ComboBox','Edit','Text','Table','RadioButton','MenuItem','List','ListItem')){
  foreach($e in (Find-All-Type $w $t)){
    try {
      $n = $e.Current.Name
      if($n -and $n.Trim().Length -gt 0){
        $r = $e.Current.BoundingRectangle
        $lines += ("{0}|{1}|en={2}|off={3}|{4}" -f $t,$n,$e.Current.IsEnabled,$e.Current.IsOffscreen,(Safe-Rect $r))
      }
    } catch {}
  }
}
$lines | Out-File -Encoding utf8 (Join-Path $base "evidence\G03-controls.txt")
"controls=$($lines.Count)"
