$ErrorActionPreference = "Continue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$LogDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("crypto_daily_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# Rotacao: mantem apenas os 14 logs mais recentes
Get-ChildItem -Path $LogDir -Filter "crypto_daily_*.log" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -Skip 14 |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -ErrorAction SilentlyContinue }

function Read-EnvValue {
  param([string]$Name)
  $envFile = Join-Path $ProjectDir ".env"
  if (-not (Test-Path -LiteralPath $envFile)) { return $null }
  $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -like "$Name=*" } | Select-Object -First 1
  if (-not $line) { return $null }
  return $line.Substring($Name.Length + 1).Trim().Trim('"').Trim("'")
}

function Send-TelegramAlert {
  param([string]$Message)
  $token = Read-EnvValue -Name "TELEGRAM_BOT_TOKEN"
  $chat = Read-EnvValue -Name "TELEGRAM_CHAT_ID"
  if (-not $token -or -not $chat) {
    Write-Warning "Sem TELEGRAM_BOT_TOKEN/CHAT_ID no .env; alerta de falha nao enviado."
    return
  }
  try {
    $body = @{ chat_id = $chat; text = $Message } | ConvertTo-Json
    Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 30 | Out-Null
  } catch {
    Write-Warning ("Falha ao enviar alerta Telegram: {0}" -f $_.Exception.Message)
  }
}

$startedAt = Get-Date
Write-Output "[$($startedAt.ToString('yyyy-MM-dd HH:mm:ss'))] Running crypto daily agent..."
Write-Output "[$($startedAt.ToString('yyyy-MM-dd HH:mm:ss'))] Running crypto daily agent..." | Out-File -FilePath $LogFile -Append -Encoding utf8

try {
  python .\crypto_daily_agent.py --send-telegram 2>&1 | Tee-Object -FilePath $LogFile -Append
  $exitCode = $LASTEXITCODE
} catch {
  $exitCode = 1
  $_ | Out-String | Tee-Object -FilePath $LogFile -Append
}

if ($exitCode -eq 0) {
  Write-Output "Done."
} else {
  Send-TelegramAlert -Message "BotMessari: o agente diario falhou (exit code $exitCode) em $(Get-Date -Format 'yyyy-MM-dd HH:mm'). Veja o log em logs/crypto_daily_$(Get-Date -Format 'yyyyMMdd').log."
  exit $exitCode
}
exit 0
