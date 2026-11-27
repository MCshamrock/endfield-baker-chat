# click.ps1 -- click inside a window (window-relative coords). Test automation only.
# usage: pwsh -File setup\tools\click.ps1 -Proc uninstall -X 554 -Y 372
param(
  [string]$Proc = "setup-shell",
  [int]$X = 10,
  [int]$Y = 10,
  [int]$Wait = 300,
  [switch]$Once
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W32C {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
}
"@

$p = Get-Process -Name $Proc -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $p) { Write-Error "no window for $Proc"; exit 1 }

[void][W32C]::SetForegroundWindow($p.MainWindowHandle)
Start-Sleep -Milliseconds 400

$r = New-Object W32C+RECT
[void][W32C]::GetWindowRect($p.MainWindowHandle, [ref]$r)
[void][W32C]::SetCursorPos($r.L + $X, $r.T + $Y)
Start-Sleep -Milliseconds 200

# click twice: the first click on a newly-activated window may only activate it
[W32C]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)
Start-Sleep -Milliseconds 70
[W32C]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)
if (-not $Once) {
  Start-Sleep -Milliseconds 250
  [W32C]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)
  Start-Sleep -Milliseconds 70
  [W32C]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)
}

Start-Sleep -Milliseconds $Wait
"clicked $Proc at ($X, $Y) of window $($r.L),$($r.T)"
