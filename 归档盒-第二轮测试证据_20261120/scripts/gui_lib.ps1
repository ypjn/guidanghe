Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class UiNative {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags,uint dx,uint dy,uint data,UIntPtr extra);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int cmd);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@
[UiNative]::SetProcessDPIAware() | Out-Null

function Get-AppWindow {
  foreach($p in @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'python*' })){
    if($p.MainWindowHandle -ne 0){
      try { return [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle) } catch {}
    }
  }
  return $null
}
function Get-ForegroundEl {
  return [System.Windows.Automation.AutomationElement]::FromHandle([UiNative]::GetForegroundWindow())
}
function Activate-App {
  $w = Get-AppWindow
  if($w){ [UiNative]::SetForegroundWindow([IntPtr]$w.Current.NativeWindowHandle) | Out-Null; Start-Sleep -Milliseconds 300 }
}
function Click-XY([int]$x,[int]$y){
  [UiNative]::SetCursorPos($x,$y) | Out-Null
  Start-Sleep -Milliseconds 120
  [UiNative]::mouse_event(0x0002,0,0,0,[UIntPtr]::Zero)
  Start-Sleep -Milliseconds 60
  [UiNative]::mouse_event(0x0004,0,0,0,[UIntPtr]::Zero)
  Start-Sleep -Milliseconds 300
}
function Click-Element($el){
  if($null -eq $el){ throw 'element is null' }
  $r = $el.Current.BoundingRectangle
  if($r.Width -le 0 -or $r.Height -le 0){ throw "element has no rect: $($el.Current.Name)" }
  Click-XY ([int]($r.X + $r.Width/2)) ([int]($r.Y + $r.Height/2))
}
function Find-Any($root,[string]$name){
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,$name)
  return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
}
function Find-NameType($root,[string]$name,[string]$type){
  $cond = New-Object System.Windows.Automation.AndCondition(
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,$name)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::$type))
  )
  return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
}
function Find-Type($root,[string]$type){
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::$type)
  return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
}
function Find-All-Type($root,[string]$type){
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::$type)
  return $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$cond)
}
function Safe-Rect($r){
  try {
    $x=[int]$r.X; $y=[int]$r.Y; $w=[int]$r.Width; $h=[int]$r.Height
    if($w -lt 0 -or $h -lt 0){ return "0,0,0,0" }
    return "$x,$y,$w,$h"
  } catch { return "?,?,?,?" }
}
function Dump-Uia($root,[int]$limit=400){
  $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
  $i=0
  foreach($e in $all){
    try {
      $n=$e.Current.Name; $t=$e.Current.ControlType.ProgrammaticName
      $en=$e.Current.IsEnabled; $off=$e.Current.IsOffscreen
      $r=Safe-Rect $e.Current.BoundingRectangle
      if($n -and $n.Trim().Length -gt 0){
        "{0} | {1} | en={2} off={3} | {4} | auto={5}" -f $t,$n,$en,$off,$r,$e.Current.AutomationId
        $i++; if($i -ge $limit){ break }
      }
    } catch { }
  }
}
function Shot([string]$path){
  $b=[System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height)
  $g=[System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size)
  $bmp.Save($path,[System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
}
function Set-TextValue($el,[string]$text){
  $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  $vp.SetValue($text)
}
function Press-Key([string]$keys){ [System.Windows.Forms.SendKeys]::SendWait($keys); Start-Sleep -Milliseconds 500 }
function Paste-Text([string]$text){
  Set-Clipboard -Value $text
  Start-Sleep -Milliseconds 200
  [System.Windows.Forms.SendKeys]::SendWait('^v')
  Start-Sleep -Milliseconds 600
}
function Wait-Window([string]$name,[int]$timeoutSec=20){
  $deadline=(Get-Date).AddSeconds($timeoutSec)
  while((Get-Date) -lt $deadline){
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,$name)
    $el=[System.Windows.Automation.AutomationElement]::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
    if($el){ return $el }
    Start-Sleep -Milliseconds 400
  }
  return $null
}
