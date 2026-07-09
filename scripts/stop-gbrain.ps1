param([int]$Port=8787)
$ErrorActionPreference='Stop'
$listener=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue|Select-Object -First 1
if(-not $listener){Write-Host 'GBrain is not running';exit 0}
$p=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
if(([string]$p.CommandLine) -notmatch 'src[/\\]cli\.ts.+serve.+--http'){throw "PID $($listener.OwningProcess) is not a verified GBrain HTTP process"}
Stop-Process -Id $listener.OwningProcess -Force
Write-Host "Stopped GBrain PID $($listener.OwningProcess)"
