# Nexus Launcher — registers MCP server, scans projects, deploys CLAUDE.md policy
# Usage: .\launcher.ps1 [-Register] [-Scan] [-Dashboard]

param(
    [switch]$Scan,
    [switch]$Dashboard,
    [switch]$Register
)

$ErrorActionPreference = "Stop"
$NexusRoot = $PSScriptRoot

Write-Host "=== Nexus Launcher ===" -ForegroundColor Cyan

# Register MCP server (user scope — available in all projects)
if ($Register) {
    Write-Host "Registering Nexus MCP server..." -ForegroundColor Yellow
    claude mcp add --transport stdio --scope user nexus -- python -m nexus serve
    Write-Host "  MCP server registered (user scope)" -ForegroundColor Green
}

# Scan all projects from nexus.toml
if ($Scan) {
    Write-Host "`nScanning projects from nexus.toml..." -ForegroundColor Yellow
    $toml = Join-Path $NexusRoot "nexus.toml"
    if (-not (Test-Path $toml)) {
        Write-Host "  nexus.toml not found. Copy nexus.toml.example and customize." -ForegroundColor Red
        exit 1
    }

    # Use Python to parse nexus.toml and scan each project
    python -c @"
from nexus.util.config import load_config
from nexus.index.pipeline import index_project
from nexus.store.db import NexusDB
from pathlib import Path

registry = load_config(Path(r'$toml'))
for name, cfg in registry.items():
    if not cfg.root.is_dir():
        print(f'  Skipping {name} (not found: {cfg.root})')
        continue
    db = NexusDB(cfg.db_path)
    result = index_project(cfg, db)
    stats = db.get_stats()
    print(f'  {name}: {stats["files"]} files, {stats["symbols"]} symbols ({result.duration_ms}ms)')
"@

    Write-Host "  Scan complete" -ForegroundColor Green
}

# Launch dashboard
if ($Dashboard) {
    Write-Host "`nStarting dashboard at http://127.0.0.1:7420" -ForegroundColor Yellow
    Start-Process "http://127.0.0.1:7420"
    python -m nexus dashboard
}

if (-not $Scan -and -not $Dashboard -and -not $Register) {
    Write-Host @"

Usage:
  .\launcher.ps1 -Register    Register Nexus as MCP server (one-time)
  .\launcher.ps1 -Scan        Scan all projects from nexus.toml
  .\launcher.ps1 -Dashboard   Start the web dashboard
  .\launcher.ps1 -Register -Scan -Dashboard   Do everything
"@ -ForegroundColor Gray
}

Write-Host "`nDone." -ForegroundColor Cyan
