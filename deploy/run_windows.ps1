# Arranque del runner autónomo en Windows (si corrés el worker en tu PC con Docker Desktop).
# Recomendado: correrlo en un VPS Linux con systemd (ver vuppia-sofia.service). En Windows,
# esta es la opción simple. Para que arranque con la sesión, programalo con el Programador de
# tareas de Windows (Task Scheduler) apuntando a este script, o instalalo como servicio con NSSM.
#
# Uso manual:  powershell -ExecutionPolicy Bypass -File deploy\run_windows.ps1
# Freno:       crear un archivo llamado VUPPIA_STOP en la carpeta worker (o cerrar la ventana).

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")   # carpeta worker

# Digest de la imagen del sandbox (obligatorio).
$env:VUPPIA_SANDBOX_DIGEST = (docker image inspect vuppia-sandbox:3 --format '{{.Id}}')

# Topes de presupuesto (red de seguridad).
$env:VUPPIA_DAILY_BUDGET_USD = "1.00"
$env:VUPPIA_MONTHLY_BUDGET_USD = "10.00"
$env:VUPPIA_PER_CALL_CAP_USD = "0.25"

# Para habilitar IA, descomentá y cargá tus claves como variables de entorno del sistema
# (no las escribas en este archivo si lo vas a compartir):
# $env:ANTHROPIC_API_KEY = "..."
# $env:VUPPIA_PROPOSER = "anthropic:claude-sonnet-5-5"
# $env:VUPPIA_SUBJECT  = "anthropic:claude-haiku-5-5"
# $env:VUPPIA_REVIEWER = "anthropic:claude-haiku-5-5"

Write-Host "VUPPIA SOFIA: iniciando runner autonomo (freno: crear archivo VUPPIA_STOP)..."
python cli.py autonomous --interval 900
