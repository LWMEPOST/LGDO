param([int]$Port=8787)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
function Import-Env($path){if(Test-Path $path){Get-Content $path|ForEach-Object{if($_ -match '^\s*([^#][^=]*)=(.*)$'){[Environment]::SetEnvironmentVariable($matches[1].Trim(),$matches[2].Trim().Trim('"').Trim("'"),'Process')}}}}
Import-Env (Join-Path $root '.env')
$listener=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue|Select-Object -First 1
if(-not $listener){Write-Host "GBrain: stopped";exit 1}
$p=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
Write-Host "GBrain: listening PID $($listener.OwningProcess)"; Write-Host "Command: $($p.CommandLine)"
Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3|Out-Null; Write-Host 'Health: OK'
if(-not $env:GBRAIN_API_KEY){Write-Host 'MCP auth: skipped';exit 0}
$body=@{jsonrpc='2.0';id=1;method='initialize';params=@{protocolVersion='2025-11-25';capabilities=@{};clientInfo=@{name='lgdo-status';version='1'}}}|ConvertTo-Json -Depth 8
Invoke-RestMethod "http://127.0.0.1:$Port/mcp" -Method Post -Headers @{Authorization="Bearer $env:GBRAIN_API_KEY";Accept='application/json, text/event-stream';'Content-Type'='application/json'} -Body $body -TimeoutSec 5|Out-Null
Write-Host 'MCP auth: OK'
