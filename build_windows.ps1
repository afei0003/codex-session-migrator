$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$specPath = Join-Path $projectRoot "CodexSessionMigrator.spec"
$distPath = Join-Path $projectRoot "dist\CodexSessionMigrator"
$zipPath = Join-Path $projectRoot "dist\CodexSessionMigrator-windows-x64.zip"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project virtual environment not found: $pythonPath. Create .venv and install dependencies first."
}

& $pythonPath -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in the project .venv. Install requirements-build.txt first."
}

& $pythonPath -m PyInstaller --clean --noconfirm $specPath
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

if (-not (Test-Path -LiteralPath (Join-Path $distPath "CodexSessionMigrator.exe"))) {
    throw "Build completed but CodexSessionMigrator.exe was not found."
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $distPath,
    $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false
)
Write-Output "Build completed: $zipPath"
