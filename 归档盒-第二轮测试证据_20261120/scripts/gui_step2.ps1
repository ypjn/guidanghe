$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
$app = Join-Path $base "app"
$py = (Get-ChildItem "C:\Users\Lenovo\Documents\Trae-CN\temp_20261119_02\test_env\*\*\runtime\python.exe" | Select-Object -First 1).FullName
$env:ARCHIVEBOX_APP_DIR = Join-Path $base "config\gui_app"
$env:ARCHIVEBOX_SCHEME_DIR = Join-Path $base "config\gui_schemes"
$env:ARCHIVEBOX_RECENT_FILE = Join-Path $base "config\gui_recent.json"
$log = Join-Path $base "logs\gui2_stdout.txt"
$err = Join-Path $base "logs\gui2_stderr.txt"
Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
$p = Start-Process -FilePath $py -ArgumentList "`"$app\main.py`"" -WorkingDirectory $app -RedirectStandardOutput $log -RedirectStandardError $err -PassThru
"pid=$($p.Id)"
. (Join-Path $base "gui_lib.ps1")
$w = Wait-Window "归档盒 V1.7" 40
if ($null -eq $w) { "main window not found"; Get-Content $err -Tail 40; exit 1 }
"found=$($w.Current.Name) rect=$($w.Current.BoundingRectangle)"
[UiNative]::SetForegroundWindow([IntPtr]$w.Current.NativeWindowHandle) | Out-Null
Start-Sleep -Milliseconds 800
Shot (Join-Path $base "shots\G02-主窗口.png")
Dump-Uia $w 800 | Out-File -Encoding utf8 (Join-Path $base "evidence\G02-uia.txt")
$lines = @()
foreach($t in @('Button','CheckBox','ComboBox','Edit','Text','Table','RadioButton','MenuItem')){
  $els = Find-All-Type $w $t
  foreach($e in $els){
    try {
      $n = $e.Current.Name
      if($n -and $n.Trim().Length -gt 0){
        $r = $e.Current.BoundingRectangle
        $lines += ("{0}|{1}|en={2}|off={3}|{4}" -f $t,$n,$e.Current.IsEnabled,$e.Current.IsOffscreen,(Safe-Rect $r))
      }
    } catch {}
  }
}
$lines | Out-File -Encoding utf8 (Join-Path $base "evidence\G02-controls.txt")
"controls=$($lines.Count)"
