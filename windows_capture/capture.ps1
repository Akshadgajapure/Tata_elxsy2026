<#
.SYNOPSIS
    Windows Mobile Hotspot Capture script using pktmon.
.DESCRIPTION
    Discovers the Wi-Fi Direct Virtual Adapter / Mobile Hotspot component,
    starts packet capture, stops capture, and exports to PCAPNG.
.PARAMETER Action
    start, stop, status, export, or discover
.PARAMETER ComponentId
    Optional pktmon component ID override (auto-discovered if omitted)
.PARAMETER OutputDir
    Directory for capture files (default: .\captures)
#>

[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "status", "export", "discover")]
    [string]$Action = "status",

    [int]$ComponentId = 0,

    [string]$OutputDir = "captures",

    [int]$PacketSize = 128
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-HotspotComponentId {
    Write-Host "[*] Discovering network components using pktmon..."
    try {
        $output = & pktmon component list 2>&1
    } catch {
        Write-Error "Failed to execute 'pktmon component list'. Ensure pktmon is installed and in PATH."
        return $null
    }

    $candidates = @()
    foreach ($line in $output) {
        $line = $line.Trim()
        if ($line -match '^(\d+)[\:\s]+(.+)$') {
            $id = [int]$matches[1]
            $name = $matches[2]
            $candidates += [PSCustomObject]@{
                Id = $id
                Name = $name
                IsHotspot = ($name -match 'Direct|Wi-Fi Direct|Hotspot|Hosted')
            }
        }
    }

    $hotspot = $candidates | Where-Object { $_.IsHotspot } | Select-Object -First 1
    if ($hotspot) {
        Write-Host "[+] Discovered Hotspot component: ID $($hotspot.Id) ($($hotspot.Name))"
        return $hotspot.Id
    }

    Write-Warning "No explicit Wi-Fi Direct component found in pktmon component list."
    return $null
}

if (!(Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

switch ($Action) {
    "discover" {
        $id = Find-HotspotComponentId
        if ($id) {
            Write-Host "Discovered Component ID: $id"
        } else {
            Write-Host "Listing all available components:"
            & pktmon component list
        }
    }

    "status" {
        Write-Host "=== Pktmon Status ==="
        & pktmon status
    }

    "start" {
        if (-not (Test-IsAdmin)) {
            Write-Error "Administrator privileges are required to start packet monitoring."
            exit 1
        }

        if ($ComponentId -le 0) {
            $ComponentId = Find-HotspotComponentId
            if (-not $ComponentId) {
                Write-Error "Could not identify hotspot component ID. Specify -ComponentId manually."
                exit 1
            }
        }

        & pktmon stop 2>$null
        & pktmon filter remove 2>$null

        $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $etlPath = Join-Path $OutputDir "hotspot_$timestamp.etl"

        Write-Host "[*] Starting pktmon capture on component $ComponentId..."
        & pktmon start --capture --comp $ComponentId --file-name (Resolve-Path $etlPath -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path -Default $etlPath) --pkt-size $PacketSize

        if ($LASTEXITCODE -eq 0) {
            Write-Host "[+] Capture started successfully. Output: $etlPath"
        } else {
            Write-Error "Failed to start capture (exit code $LASTEXITCODE)."
        }
    }

    "stop" {
        Write-Host "[*] Stopping pktmon capture..."
        & pktmon stop
        Write-Host "[+] Capture stopped."
    }

    "export" {
        $latestEtl = Get-ChildItem -Path $OutputDir -Filter "hotspot_*.etl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latestEtl) {
            Write-Error "No .etl capture files found in $OutputDir"
            exit 1
        }

        $pcapngPath = [System.IO.Path]::ChangeExtension($latestEtl.FullName, ".pcapng")
        Write-Host "[*] Converting $($latestEtl.FullName) to $pcapngPath..."
        & pktmon pcapng $latestEtl.FullName -o $pcapngPath

        if (Test-Path $pcapngPath) {
            Write-Host "[+] Conversion complete: $pcapngPath"
        } else {
            Write-Error "Conversion to PCAPNG failed."
        }
    }
}
