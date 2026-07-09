param([int]$GBrainPort=8787,[int]$AppPort=8000,[switch]$SkipApp)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$repo=Join-Path $root 'gbrain'; $gbrainHome=Join-Path $root 'data\gbrain'; $out=Join-Path $root 'output'
$health="http://127.0.0.1:$GBrainPort/health"
function Import-Env($path){if(Test-Path $path){Get-Content $path|ForEach-Object{if($_ -match '^\s*([^#][^=]*)=(.*)$'){[Environment]::SetEnvironmentVariable($matches[1].Trim(),$matches[2].Trim().Trim('"').Trim("'"),'Process')}}}}
function Healthy{try{(Invoke-WebRequest -UseBasicParsing $health -TimeoutSec 2).StatusCode -eq 200}catch{$false}}
Import-Env (Join-Path $root '.env'); Import-Env (Join-Path $gbrainHome '.gbrain\.env'); $env:GBRAIN_HOME=$gbrainHome
New-Item -ItemType Directory -Force $out|Out-Null
if(-not (Healthy)){
  $listener=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $GBrainPort -State Listen -ErrorAction SilentlyContinue|Select-Object -First 1
  if($listener){$p=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)";throw "Port $GBrainPort occupied by PID $($listener.OwningProcess): $($p.CommandLine)"}
  $bun=(Get-Command bun.cmd -ErrorAction Stop).Source
  Start-Process $env:ComSpec -ArgumentList @('/d','/s','/c',"`"$bun`" run src/cli.ts serve --http --bind 127.0.0.1 --port $GBrainPort --suppress-bootstrap-token") -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $out 'gbrain-http.stdout.log') -RedirectStandardError (Join-Path $out 'gbrain-http.stderr.log')|Out-Null
  $deadline=(Get-Date).AddSeconds(20); do{Start-Sleep -Milliseconds 500;if(Healthy){break}}while((Get-Date)-lt $deadline)
  if(-not (Healthy)){throw 'GBrain did not become healthy. See output/gbrain-http.stderr.log'}
  Write-Host "GBrain HTTP MCP started on 127.0.0.1:$GBrainPort"
}else{Write-Host "GBrain HTTP MCP already healthy on 127.0.0.1:$GBrainPort"}
if(-not $SkipApp){Push-Location $root;try{python -m uvicorn app.main:app --host 127.0.0.1 --port $AppPort --reload}finally{Pop-Location}}
