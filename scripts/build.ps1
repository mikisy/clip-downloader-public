$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
# Always build side by side: WebView2 or Explorer may keep the old folder open.
$outputRoot = Join-Path (Join-Path (Get-Location) 'dist') ('build-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
uv run --locked pyinstaller --noconfirm --clean --onedir --windowed --name ClipDownloader --distpath $outputRoot --paths src --add-binary ".venv/Scripts/deno.exe;." --collect-all deno --collect-all yt_dlp_ejs --collect-all imageio_ffmpeg --collect-all yt_dlp --copy-metadata yt-dlp --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime --collect-all tokenizers --collect-all huggingface_hub --collect-all nvidia.cuda_runtime --collect-all nvidia.cuda_nvrtc --collect-all nvidia.cublas --collect-all nvidia.cudnn src/clip_downloader/app.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
$exe = Join-Path $outputRoot 'ClipDownloader\ClipDownloader.exe'
@{ executable = $exe; built_at = (Get-Date -Format o) } | ConvertTo-Json | Set-Content -Encoding UTF8 'dist\latest-build.json'
Write-Host "Output: $exe (distribute the entire folder)"
