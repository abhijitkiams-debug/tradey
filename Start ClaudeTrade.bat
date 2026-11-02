@echo off
REM Double-click me (Windows). Starts ClaudeTrade and opens it in your browser, already signed in.
cd /d "%~dp0"
echo Starting ClaudeTrade... (the first time takes a few minutes)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "docker info *> $null; if ($LASTEXITCODE -ne 0) { Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe' -ErrorAction SilentlyContinue; for ($i=0; $i -lt 90; $i++) { Start-Sleep 2; docker info *> $null; if ($LASTEXITCODE -eq 0) { break } } }" ^
  "docker info *> $null; if ($LASTEXITCODE -ne 0) { Write-Host 'Docker is not running. Install/open Docker Desktop, then try again.'; exit 1 }" ^
  "if (-not (Test-Path .env)) { $b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); 'CLAUDETRADE_OPERATOR_TOKEN=' + (-join ($b | ForEach-Object { $_.ToString('x2') })) | Set-Content -Encoding ascii .env }" ^
  "if (-not (Test-Path config\claudetrade.toml)) { Copy-Item config\claudetrade.example.toml config\claudetrade.toml }" ^
  "docker compose up -d --build; if ($LASTEXITCODE -ne 0) { Write-Host 'Could not start. Take a photo of this window and ask for help.'; exit 1 }" ^
  "for ($i=0; $i -lt 90; $i++) { try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/healthz | Out-Null; break } catch { Start-Sleep 2 } }" ^
  "$t = (Select-String -Path .env -Pattern '^CLAUDETRADE_OPERATOR_TOKEN=(.*)').Matches[0].Groups[1].Value; Start-Process ('http://127.0.0.1:8000/#token=' + $t)"
echo ClaudeTrade is open in your browser. You can close this window.
pause
