# One-time local setup on Windows (PowerShell). Requires Docker Desktop.
# Writes .env (asks for your Groww TOTP token + secret; the secret is typed hidden), creates the
# config from the example, runs the pre-flight checks and starts paper trading.
#   powershell -ExecutionPolicy Bypass -File deploy\setup-local.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Docker is not installed. Install Docker Desktop: https://docs.docker.com/get-docker/ then re-run."; exit 1
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "Start Docker Desktop, wait until it is running, then re-run."; exit 1 }

if (Test-Path .env) {
  Write-Host ".env already exists - keeping it (delete it to start over)"
} else {
  Write-Host @"
Groww Trade API (needs an active Trading API subscription on Groww). In your browser:
  1. Open https://groww.in/trade-api/api-keys and sign in.
  2. Next to "Generate API Key", open the dropdown and choose "Generate TOTP token".
  3. Name it (e.g. claudetrade) and continue.
  4. Copy the TOTP token and the TOTP secret. Do not share them with anyone, including in chat.
"@
  $key = Read-Host "Paste the Groww TOTP token"
  $sec = Read-Host "Paste the Groww TOTP secret (input hidden)" -AsSecureString
  $secret = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  $secret = ($secret -replace '\s', '').ToUpper()
  if (-not $key -or $secret -notmatch '^[A-Z2-7]+=*$') { Write-Host "Missing token, or the secret isn't a TOTP secret (A-Z, 2-7)."; exit 1 }
  $tg = Read-Host "Telegram bot token (Enter to skip)"
  $chat = ""; if ($tg) { $chat = Read-Host "Telegram chat id" }
  $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $op = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  @"
# Written by deploy/setup-local.ps1 - keep private. Never commit.
CLAUDETRADE_OPERATOR_TOKEN=$op
GROWW_API_KEY=$key
GROWW_TOTP_SECRET=$secret
GROWW_API_SECRET=
GROWW_ACCESS_TOKEN=
CLAUDETRADE_TELEGRAM_BOT_TOKEN=$tg
CLAUDETRADE_TELEGRAM_CHAT_ID=$chat
CLAUDETRADE_ALERT_WEBHOOK_URL=
CLAUDETRADE_ALERT_MIN_SEVERITY=HIGH
ANTHROPIC_API_KEY=
"@ | Set-Content -Encoding ascii .env
  icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)" *> $null
  Remove-Variable secret, sec
  Write-Host "Wrote .env (readable only by you)."
}

if (-not (Test-Path config\claudetrade.toml)) {
  Copy-Item config\claudetrade.example.toml config\claudetrade.toml
  Write-Host "Created config\claudetrade.toml (limits Rs100 / Rs300 / Rs1,000; paper only). Add NSE holidays before trading."
}

docker compose build
docker compose run --rm --entrypoint claudetrade desk doctor --config /config/claudetrade.toml --send-test-alert
if ($LASTEXITCODE -ne 0) { Write-Host "Pre-flight checks failed - fix the FAIL lines above and re-run."; exit 1 }
docker compose up -d
$tok = (Select-String -Path .env -Pattern '^CLAUDETRADE_OPERATOR_TOKEN=(.*)').Matches[0].Groups[1].Value
Write-Host "`nDashboard: http://127.0.0.1:8000`nOperator token (sign-in): $tok"
Write-Host "Keep this PC awake and online on trading days 09:00-16:30 IST (Settings > Power: never sleep while plugged in)."
