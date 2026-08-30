$ErrorActionPreference = 'Stop'

$awakeExeCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'PowerToys\PowerToys.Awake.exe'),
    (Join-Path $env:ProgramFiles 'PowerToys\PowerToys.Awake.exe')
)
$awakeExe = $awakeExeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $awakeExe) {
    throw 'PowerToys.Awake.exe was not found.'
}

$settingsDirectory = Join-Path $env:LOCALAPPDATA 'Microsoft\PowerToys\Awake'
$settingsPath = Join-Path $settingsDirectory 'settings.json'
if (-not (Test-Path -LiteralPath $settingsPath)) {
    throw "PowerToys Awake settings were not found: $settingsPath"
}

$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
if (-not $settings.properties) {
    throw 'The PowerToys Awake settings file has no properties object.'
}

$settings.properties.keepDisplayOn = $true
$settings.properties.mode = 2
$settings.properties.intervalHours = 15
$settings.properties.intervalMinutes = 0

$json = $settings | ConvertTo-Json -Depth 20 -Compress
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($settingsPath, $json, $utf8WithoutBom)

$awakeProcess = Get-Process -Name 'PowerToys.Awake' -ErrorAction SilentlyContinue
if (-not $awakeProcess) {
    Start-Process -FilePath $awakeExe -ArgumentList '--use-pt-config' -WorkingDirectory (Split-Path -Parent $awakeExe) -WindowStyle Hidden
}

Write-Host 'PowerToys Awake is set for 15 hours with the screen kept on.'
