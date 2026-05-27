$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

python .\crypto_daily_agent.py --send-telegram
exit $LASTEXITCODE
