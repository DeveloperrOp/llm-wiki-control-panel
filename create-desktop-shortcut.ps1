# Создаёт ярлык «LLM Wiki Dashboard» на рабочем столе.
# Двойной клик по ярлыку запускает Flask + открывает браузер.
#
# Запуск:
#   Правый клик на этом файле → «Выполнить с помощью PowerShell»
# Или:
#   powershell -ExecutionPolicy Bypass -File create-desktop-shortcut.ps1

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$target = Join-Path $scriptDir 'start-dashboard.bat'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = Join-Path $desktop 'LLM Wiki Dashboard.lnk'

if (-not (Test-Path $target)) {
    Write-Error "Не найден start-dashboard.bat по пути: $target"
    exit 1
}

# Пытаемся найти какую-нибудь иконку. Если есть python.exe — берём её.
$iconPath = ''
try {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) { $iconPath = $py }
} catch {}

$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($shortcut)
$lnk.TargetPath = $target
$lnk.WorkingDirectory = $scriptDir
$lnk.Description = 'LLM Wiki Control Panel — http://localhost:5757'
$lnk.WindowStyle = 1  # Normal
if ($iconPath) { $lnk.IconLocation = "$iconPath,0" }
$lnk.Save()

Write-Host "✅ Ярлык создан на рабочем столе:"
Write-Host "   $shortcut"
Write-Host ""
Write-Host "Двойной клик по ярлыку запустит dashboard на http://localhost:5757"
