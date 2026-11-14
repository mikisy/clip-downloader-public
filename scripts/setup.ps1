$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'
}
uv python install 3.12
if ($LASTEXITCODE -ne 0) { throw 'Python installation failed' }
uv sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
uv run --locked clip-downloader --check
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed' }
