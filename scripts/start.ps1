$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
uv run --locked clip-downloader
if ($LASTEXITCODE -ne 0) { throw 'Application failed' }
