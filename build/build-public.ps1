$ErrorActionPreference = 'Stop'
$publicBuildRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $publicBuildRoot
python -m PyInstaller --noconfirm --distpath dist/public --workpath build/work-public build/TSearchDS.spec
if ($LASTEXITCODE -ne 0) { throw 'Public build failed' }
Write-Output 'EXE: dist/public/TSearchDS.exe. Place an official mihomo.exe beside it and create empty portable.txt and run/ for portable use.'
