$ErrorActionPreference = "Continue"

param(
  [string]$At = "07:10",
  [switch]$RunImmediately
)

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

function Get-NextRun {
  param(
    [string]$TimeText,
    [Nullable[datetime]]$LastRun
  )

  $runTime = [TimeSpan]::Parse($TimeText)
  $now = Get-Date
  $candidate = $now.Date.Add($runTime)

  if ($candidate -le $now) {
    $candidate = $candidate.AddDays(1)
  }

  if ($LastRun.HasValue -and $LastRun.Value.Date -eq $candidate.Date) {
    $candidate = $candidate.AddDays(1)
  }

  return $candidate
}

function Invoke-DailyAgent {
  $startedAt = Get-Date
  Write-Host "[$($startedAt.ToString('yyyy-MM-dd HH:mm:ss'))] Running crypto daily agent..."

  python .\crypto_daily_agent.py --send-telegram
  $exitCode = $LASTEXITCODE

  if ($exitCode -eq 0) {
    Write-Host "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] Done."
  } else {
    Write-Warning "Crypto daily agent exited with code $exitCode. The loop will keep running."
  }

  return $startedAt
}

$lastRun = $null
Write-Host "BotMessari crypto dev loop started. Daily run time: $At. Press Ctrl+C to stop."

if ($RunImmediately) {
  $lastRun = Invoke-DailyAgent
}

while ($true) {
  $nextRun = Get-NextRun -TimeText $At -LastRun $lastRun
  $seconds = [Math]::Max(1, [int][Math]::Ceiling(($nextRun - (Get-Date)).TotalSeconds))

  Write-Host "Next run: $($nextRun.ToString('yyyy-MM-dd HH:mm:ss')). Waiting..."
  Start-Sleep -Seconds $seconds

  $lastRun = Invoke-DailyAgent
}
