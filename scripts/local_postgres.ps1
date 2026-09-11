<#
.SYNOPSIS
    Run PostgreSQL on Windows WITHOUT admin rights and WITHOUT Docker.

.DESCRIPTION
    Uses the official PostgreSQL "binaries" zip (not the installer), which is
    just files in a folder — no service, no registry, no elevation. Everything
    lands under -Root, which defaults to your user profile.

    This is a LOCAL DEVELOPMENT convenience only. Production Postgres runs in
    the container defined in docker-compose.yml, on the gateway VM.

.EXAMPLE
    .\scripts\local_postgres.ps1 setup     # download, init, start, load schema
    .\scripts\local_postgres.ps1 start
    .\scripts\local_postgres.ps1 stop
    .\scripts\local_postgres.ps1 status
    .\scripts\local_postgres.ps1 psql      # interactive shell
    .\scripts\local_postgres.ps1 migrate   # apply db\migrations\*.sql (keeps data)
    .\scripts\local_postgres.ps1 reset     # DESTROYS data, reloads schema
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'start', 'stop', 'status', 'psql', 'reset', 'migrate')]
    [string]$Action = 'setup',

    # Check https://www.enterprisedb.com/download-postgresql-binaries for the
    # current version if this one 404s.
    [string]$Version = '16.6-1',

    [string]$Root = "$env:USERPROFILE\pgsql-llmaas",

    # Use an already-downloaded zip instead of fetching one.
    [string]$ZipPath = ''
)

$ErrorActionPreference = 'Stop'

$BinDir  = Join-Path $Root "pgsql\bin"
$DataDir = Join-Path $Root "data"
$LogFile = Join-Path $Root "postgres.log"
$Port    = 5432
$DbUser  = 'llmaas'
$DbPass  = 'llmaas'
$DbName  = 'llmaas'
$RepoRoot = Split-Path $PSScriptRoot -Parent

function Invoke-Pg([string]$Exe, [string[]]$PgArgs) {
    $env:PGPASSWORD = $DbPass
    & (Join-Path $BinDir $Exe) @PgArgs
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

function Install-Binaries {
    if (Test-Path (Join-Path $BinDir "postgres.exe")) {
        Write-Host "PostgreSQL binaries already present at $BinDir" -ForegroundColor Green
        return
    }
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $zip = $ZipPath
    if (-not $zip) {
        $zip = Join-Path $Root "postgresql-$Version-windows-x64-binaries.zip"
        $url = "https://get.enterprisedb.com/postgresql/postgresql-$Version-windows-x64-binaries.zip"
        Write-Host "Downloading $url" -ForegroundColor Cyan
        Write-Host "(~350 MB, one time)" -ForegroundColor DarkGray
        try {
            # Progress bar makes Invoke-WebRequest ~10x slower on large files.
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        } catch {
            throw @"
Download failed: $($_.Exception.Message)

Version '$Version' may no longer be published. Either:
  1. Pick a current version from
     https://www.enterprisedb.com/download-postgresql-binaries
     and re-run with:  .\scripts\local_postgres.ps1 setup -Version 17.2-1
  2. Or download the zip by hand and re-run with:
     .\scripts\local_postgres.ps1 setup -ZipPath C:\path\to\file.zip
"@
        }
    }
    Write-Host "Extracting to $Root" -ForegroundColor Cyan
    Expand-Archive -Path $zip -DestinationPath $Root -Force
}

function Initialize-Cluster {
    if (Test-Path (Join-Path $DataDir "PG_VERSION")) {
        Write-Host "Cluster already initialised at $DataDir" -ForegroundColor Green
        return
    }
    $pwFile = Join-Path $env:TEMP "pgpw_$(Get-Random).txt"
    try {
        Set-Content -Path $pwFile -Value $DbPass -Encoding ascii -NoNewline
        Write-Host "Initialising cluster at $DataDir" -ForegroundColor Cyan
        Invoke-Pg 'initdb.exe' @('-D', $DataDir, '-U', $DbUser, "--pwfile=$pwFile",
                                 '--auth=scram-sha-256', '--encoding=UTF8')
    } finally {
        Remove-Item $pwFile -Force -ErrorAction SilentlyContinue
    }
}

function Assert-Installed {
    if (-not (Test-Path (Join-Path $BinDir "pg_ctl.exe"))) {
        throw "PostgreSQL is not set up at $Root. Run:  .\scripts\local_postgres.ps1 setup"
    }
}

function Test-Running {
    & (Join-Path $BinDir "pg_ctl.exe") -D $DataDir status *> $null
    return ($LASTEXITCODE -eq 0)
}

function Start-Server {
    if (Test-Running) { Write-Host "Already running on port $Port" -ForegroundColor Green; return }
    Write-Host "Starting PostgreSQL on 127.0.0.1:$Port" -ForegroundColor Cyan
    # Bind to loopback only: this cluster has a dev password and no TLS.
    Invoke-Pg 'pg_ctl.exe' @('-D', $DataDir, '-l', $LogFile, '-o', "-p $Port -h 127.0.0.1", '-w', 'start')
}

function Stop-Server {
    if (-not (Test-Running)) { Write-Host "Not running." -ForegroundColor Yellow; return }
    Invoke-Pg 'pg_ctl.exe' @('-D', $DataDir, '-m', 'fast', '-w', 'stop')
    Write-Host "Stopped." -ForegroundColor Green
}

function Initialize-Schema {
    $env:PGPASSWORD = $DbPass
    # createdb exits non-zero if the database already exists; that is fine.
    & (Join-Path $BinDir "createdb.exe") -h 127.0.0.1 -p $Port -U $DbUser $DbName *> $null
    Write-Host "Applying db\init.sql" -ForegroundColor Cyan
    Invoke-Pg 'psql.exe' @('-h', '127.0.0.1', '-p', $Port, '-U', $DbUser, '-d', $DbName,
                           '-v', 'ON_ERROR_STOP=1', '-q', '-f', (Join-Path $RepoRoot "db\init.sql"))
}

if ($Action -ne 'setup') { Assert-Installed }

switch ($Action) {
    'setup' {
        Install-Binaries
        Initialize-Cluster
        Start-Server
        Initialize-Schema
        Write-Host ""
        Write-Host "PostgreSQL is ready." -ForegroundColor Green
        Write-Host "Put this in your .env:" -ForegroundColor Green
        Write-Host "  DATABASE_URL=postgresql://${DbUser}:${DbPass}@127.0.0.1:${Port}/${DbName}"
    }
    'start'  { Start-Server }
    'stop'   { Stop-Server }
    'status' {
        if (Test-Running) { Write-Host "running on 127.0.0.1:$Port" -ForegroundColor Green }
        else { Write-Host "stopped" -ForegroundColor Yellow }
    }
    'psql'   {
        $env:PGPASSWORD = $DbPass
        & (Join-Path $BinDir "psql.exe") -h 127.0.0.1 -p $Port -U $DbUser -d $DbName
    }
    'migrate' {
        # Apply db\migrations\*.sql in order, keeping existing data.
        # db\init.sql only ever runs on a fresh database, so this is how an
        # already-created one picks up a schema change. Each file is written
        # to be idempotent, so re-running is safe.
        Assert-Installed
        if (-not (Test-Running)) { Start-Server }
        $env:PGPASSWORD = $DbPass
        $dir = Join-Path $RepoRoot "db\migrations"
        if (-not (Test-Path $dir)) { Write-Host "No db\migrations directory."; break }
        $files = Get-ChildItem -Path $dir -Filter *.sql | Sort-Object Name
        if (-not $files) { Write-Host "No migrations to apply."; break }
        foreach ($f in $files) {
            Write-Host "Applying $($f.Name)" -ForegroundColor Cyan
            Invoke-Pg 'psql.exe' @('-h', '127.0.0.1', '-p', $Port, '-U', $DbUser,
                                   '-d', $DbName, '-v', 'ON_ERROR_STOP=1', '-q',
                                   '-f', $f.FullName)
        }
        Write-Host "Migrations applied." -ForegroundColor Green
    }

    'reset'  {
        Write-Host "This DELETES all local data in $DbName." -ForegroundColor Yellow
        $answer = Read-Host "Type 'yes' to continue"
        if ($answer -ne 'yes') { Write-Host "Aborted."; break }
        $env:PGPASSWORD = $DbPass
        & (Join-Path $BinDir "dropdb.exe") -h 127.0.0.1 -p $Port -U $DbUser --if-exists $DbName
        Initialize-Schema
        Write-Host "Reset complete." -ForegroundColor Green
    }
}
