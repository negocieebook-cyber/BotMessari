$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
python .\x_generator.py --send-telegram
exit $LASTEXITCODE
