$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = Join-Path $ProjectDir "run_x.ps1"
$TaskName = "BotMessariXPosts"

# Dois horarios por dia (padrao manha 07:15 e tarde 18:00).
# Altere via argumento:  -Morning "07:30"  -Afternoon  "17:00"
param(
  [string]$Morning   = "07:15",
  [string]$Afternoon = "18:00"
)

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
