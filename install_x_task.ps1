param(
  [string]$Morning   = "07:15",
  [string]$Afternoon = "18:00"
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = Join-Path $ProjectDir "run_x.ps1"
$TaskName = "BotMessariXPosts"

# Horarios default: manha 07:15 e tarde 18:00.
# Para mudar: install_x_task.ps1 -Morning "08:00" -Afternoon "19:00"

$Action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

$TriggerMorning   = New-ScheduledTaskTrigger -Daily -At $Morning
$TriggerAfternoon = New-ScheduledTaskTrigger -Daily -At $Afternoon

$Settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger @($TriggerMorning, $TriggerAfternoon) `
  -Settings $Settings `
  -Description "Gera 2 posts de cripto prontos para o X (@bpweb33) e envia por Telegram para revisao." `
  -Force | Out-Null

Write-Host "Scheduled task installed: $TaskName ($Morning e $Afternoon)"
