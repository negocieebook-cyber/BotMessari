$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

python .\messari_daily_agent.py --send-telegram
exit $LASTEXITCODE
