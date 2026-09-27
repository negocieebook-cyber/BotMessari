$ErrorActionPreference = "Continue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$LogDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("x_posts_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# Rotacao: mantem apenas os 14 logs mais recentes
Get-ChildItem -Path $LogDir -Filter "x_posts_*.log" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -Skip 14 |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -ErrorAction SilentlyContinue }

Write-Output "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] Running X post generator..." | Out-File -FilePath $LogFile -Append -Encoding utf8

try {
  python .\x_generator.py --send-telegram 2>&1 | Tee-Object -FilePath $LogFile -Append
  $exitCode = $LASTEXITCODE
} catch {
  $exitCode = 1
  $_ | Out-String | Tee-Object -FilePath $LogFile -Append
}

if ($exitCode -ne 0) {
  $token = $null; $chat = $null
  Get-Content (Join-Path $ProjectDir ".env") | ForEach-Object {
    if ($_ -like "TELEGRAM_BOT_TOKEN=*") { $token = $_.Substring(19).Trim().Trim('"').Trim("'") }
    if ($_ -like "TELEGRAM_CHAT_ID=*") { $chat = $_.Substring(17).Trim().Trim('"').Trim("'") }
  }
  if ($token -and $chat) {
    try {
      $body = @{ chat_id = $chat; text = "BotMessari: o gerador de posts para o X falhou (exit code $exitCode). Veja o log em logs/x_posts_$(Get-Date -Format 'yyyyMMdd').log." } | ConvertTo-Json
      Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 30 | Out-Null
    } catch { }
  }
}
exit $exitCode
