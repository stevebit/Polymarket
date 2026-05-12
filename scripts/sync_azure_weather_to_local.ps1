<#
.SYNOPSIS
    Dump Azure Postgres `weather` DB (WEATHER_POSTGRES_URL) and restore into a local DB.

.DESCRIPTION
    Requires pg_dump / pg_restore from a local PostgreSQL install (same major
    version as Azure 16+ is ideal; 16 or 17 usually works).

    Set WEATHER_POSTGRES_URL to your Azure libpq URL in this shell before running
    (e.g. paste from .env — do not commit).

.PARAMETER PostgresBin
    Directory containing pg_dump.exe and pg_restore.exe.

.PARAMETER LocalUrl
    libpq URL for the target local database, e.g.
    postgresql://postgres:secret@127.0.0.1:5432/weather_local?sslmode=disable

.PARAMETER DumpPath
    Where to write the custom-format dump (default: repo data/azure_weather.dump).

.EXAMPLE
    $env:WEATHER_POSTGRES_URL = "postgresql://weatheradmin:...@....postgres.database.azure.com:5432/weather?sslmode=require"
    .\scripts\sync_azure_weather_to_local.ps1 -PostgresBin "C:\Program Files\PostgreSQL\18\bin" `
      -LocalUrl "postgresql://postgres:localpw@127.0.0.1:5432/weather_local?sslmode=disable"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PostgresBin,

    [Parameter(Mandatory = $true)]
    [string]$LocalUrl,

    [string]$DumpPath = ""
)

$ErrorActionPreference = "Stop"

if (-not $env:WEATHER_POSTGRES_URL) {
    throw "WEATHER_POSTGRES_URL is not set. Set it to your Azure connection string for this session (from .env)."
}

$dumpExe = Join-Path $PostgresBin "pg_dump.exe"
$restoreExe = Join-Path $PostgresBin "pg_restore.exe"
if (-not (Test-Path $dumpExe)) { throw "Not found: $dumpExe" }
if (-not (Test-Path $restoreExe)) { throw "Not found: $restoreExe" }

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $DumpPath) {
    $dataDir = Join-Path $repoRoot "data"
    if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
    $DumpPath = Join-Path $dataDir "azure_weather.dump"
}

Write-Host "Dumping Azure → $DumpPath"
& $dumpExe $env:WEATHER_POSTGRES_URL -Fc -f $DumpPath --no-owner --no-acl -v
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit $LASTEXITCODE" }

Write-Host "Restoring into local database (clean)…"
& $restoreExe --dbname $LocalUrl --verbose --clean --if-exists --no-owner $DumpPath
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pg_restore exit $LASTEXITCODE — partial restore is common if extensions differ. Install pgcrypto on local DB and retry."
}

Write-Host "Done. Dump: $DumpPath"
Write-Host "Tip: use pgAdmin/DBeaver against the LocalUrl host/db to browse tables."
