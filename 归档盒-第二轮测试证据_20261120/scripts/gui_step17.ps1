$ErrorActionPreference = "Stop"
$base = "C:\Users\Lenovo\Documents\Trae-CN\temp_20261120_01\round2"
$app = Join-Path $base "app"
$py = (Get-ChildItem "C:\Users\Lenovo\Documents\Trae-CN\temp_20261119_02\test_env\*\*\runtime\python.exe" | Select-Object -First 1).FullName
$env:ARCHIVEBOX_APP_DIR = Join-Path $base "config\gui_app"
$env:ARCHIVEBOX_SCHEME_DIR = Join-Path $base "config\gui_schemes"
$env:ARCHIVEBOX_RECENT_FILE = Join-Path $base "config\gui_recent.json"
$p = Start-Process -FilePath $py -ArgumentList "`"$app\main.py`"" -WorkingDirectory $app -RedirectStandardOutput (Join-Path $base "logs\gui4_stdout.txt") -RedirectStandardError (Join-Path $base "logs\gui4_stderr.txt") -PassThru
"pid=$($p.Id)"
$proc = $null
for($i=0;$i -lt 60;$i++){ Start-Sleep -Milliseconds 500; $proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue; if($proc -and $proc.MainWindowHandle -ne 0){ break } }
if(-not $proc -or $proc.MainWindowHandle -eq 0){ "window not ready"; exit 1 }
. (Join-Path $base "gui_lib.ps1")
$w = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$proc.MainWindowHandle)
[UiNative]::SetForegroundWindow([IntPtr]$proc.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 800
Shot (Join-Path $base "shots\G28-单实例启动.png")
$r = Find-NameType $w "最近打开" "Button"
"recent_rect=$(Safe-Rect $r.Current.BoundingRectangle)"
Click-Element $r
Start-Sleep -Seconds 2
Shot (Join-Path $base "shots\G29-最近打开.png")
$radios = Find-All-Type $w "RadioButton"
"radios=$($radios.Count)"
foreach($e in $radios){ try { "RADIO|$($e.Current.Name)|$(Safe-Rect $e.Current.BoundingRectangle)" } catch {} }
if($radios.Count -gt 0){
  Click-Element $radios.Item(0)
  Start-Sleep -Milliseconds 500
  $open = Find-NameType $w "打开空间" "Button"
  "open_enabled=$($open.Current.IsEnabled)"
  if($open.Current.IsEnabled){ Click-Element $open; Start-Sleep -Seconds 2 }
}
foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and ($n -match "已打开|尚未扫描|本次范围|共扫描")){ "TXT|$n" } } catch {} }
# 扫描并打开配置
$s = Find-NameType $w "重新扫描" "Button"
"scan_enabled=$($s.Current.IsEnabled)"
Click-Element $s
Start-Sleep -Seconds 6
$cfg = Find-NameType $w "学期和目录规则" "Button"
"cfg_enabled=$($cfg.Current.IsEnabled) rect=$(Safe-Rect $cfg.Current.BoundingRectangle)"
if($cfg.Current.IsEnabled){
  Click-Element $cfg
  Start-Sleep -Seconds 2
  Shot (Join-Path $base "shots\G30-分类配置.png")
  foreach($e in (Find-All-Type $w "TabItem")){ try { "TAB|$($e.Current.Name)" } catch {} }
  Dump-Uia $w 600 | Out-File -Encoding utf8 (Join-Path $base "evidence\G30-cfg-uia.txt")
  foreach($e in (Find-All-Type $w "Text")){ try { $n=$e.Current.Name; if($n -and $n.Length -gt 2 -and $n.Length -lt 50){ "CTXT|$n" } } catch {} }
  foreach($n in @("关闭","取消")){ $b=Find-NameType $w $n "Button"; if($b){ Click-Element $b; "closed_by=$n"; break } }
}
