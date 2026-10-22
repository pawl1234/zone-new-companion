<#
.SYNOPSIS
    Builds dist\zone-new-companion.exe: one self-contained file with libmpv inside.

.DESCRIPTION
    Creates the venv if needed, installs requirements.txt, fetches libmpv-2.dll
    from shinchiro's mpv builds (cached in build\libmpv) and runs PyInstaller.
    Unpacking the mpv archive needs 7-Zip or a Windows tar.exe with LZMA
    support (Windows 11); otherwise pass -Libmpv with a libmpv-2.dll you got yourself.

.PARAMETER Libmpv
    Path to a libmpv-2.dll to bundle instead of downloading one.

.PARAMETER NoLibmpv
    Build without the built-in player; the app then plays everything in VLC.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#>
[CmdletBinding()]
param(
    [string]$Libmpv,
    [switch]$NoLibmpv
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar
# Windows PowerShell 5.1 may still default to TLS 1.0, which GitHub refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
$Name = 'zone-new-companion'
$MpvRepo = 'shinchiro/mpv-winbuild-cmake'
$MpvCache = Join-Path $Root 'build\libmpv'

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

function Find-Python {
    # The app needs Python 3.10+ (dataclass slots); prefer the py launcher's newest.
    foreach ($candidate in @(@('py', '-3'), @('python'))) {
        $exe = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $pyArgs = @($candidate | Select-Object -Skip 1)
        try {
            # The Microsoft Store "python" stub fails here, as it should.
            $version = & $exe.Source @pyArgs -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -eq 0 -and $version -and [version]$version -ge [version]'3.10') {
            return , (@($exe.Source) + $pyArgs)
        }
    }
    throw 'Python 3.10 or newer not found. Install it with: winget install Python.Python.3.13'
}

function Find-SevenZip {
    $command = Get-Command 7z.exe, 7za.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    foreach ($dir in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($dir -and (Test-Path "$dir\7-Zip\7z.exe")) { return "$dir\7-Zip\7z.exe" }
    }
    return $null
}

function Expand-Libmpv {
    param([string]$Archive, [string]$Destination)
    $sevenZip = Find-SevenZip
    if ($sevenZip) {
        Invoke-Checked $sevenZip @('e', '-y', "-o$Destination", $Archive, 'libmpv-2.dll')
        return
    }
    # The mpv archives use LZMA2 + BCJ2; Windows 10's tar.exe lacks LZMA, Windows 11's has it.
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if ((Test-Path $tar) -and ((& $tar --version) -match 'liblzma')) {
        Invoke-Checked $tar @('-xf', $Archive, '-C', $Destination, 'libmpv-2.dll')
        return
    }
    throw "Cannot unpack $Archive. Install 7-Zip (winget install 7zip.7zip) and run again, or pass -Libmpv <path to libmpv-2.dll>."
}

function Get-Libmpv {
    $dll = Join-Path $MpvCache 'libmpv-2.dll'
    if (Test-Path $dll) {
        Write-Host "Using cached $dll (delete build\libmpv to fetch a newer one)"
        return $dll
    }
    New-Item -ItemType Directory -Force -Path $MpvCache | Out-Null
    Write-Host "Looking up the latest libmpv release of $MpvRepo..."
    $release = Invoke-RestMethod "https://api.github.com/repos/$MpvRepo/releases/latest" -Headers @{ 'User-Agent' = $Name }
    # Plain x86_64, not the -v3 build, which needs a CPU with AVX2.
    $asset = $release.assets | Where-Object { $_.name -match '^mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z$' } | Select-Object -First 1
    if (-not $asset) { throw "No mpv-dev-x86_64 archive in release $($release.tag_name) of $MpvRepo." }
    $archive = Join-Path $MpvCache $asset.name
    Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB)..."
    Invoke-WebRequest $asset.browser_download_url -OutFile $archive -Headers @{ 'User-Agent' = $Name }
    Expand-Libmpv -Archive $archive -Destination $MpvCache
    Remove-Item $archive
    if (-not (Test-Path $dll)) { throw "libmpv-2.dll missing from $($asset.name)." }
    return $dll
}

Push-Location $Root
try {
    $venvPython = Join-Path $Root 'venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        $python = Find-Python
        Write-Host "Creating venv with $($python -join ' ')..."
        Invoke-Checked $python[0] (@($python | Select-Object -Skip 1) + @('-m', 'venv', 'venv'))
    }
    Write-Host 'Installing requirements...'
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '--quiet', '-r', 'requirements.txt')

    $pyinstallerArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', $Name,
        '--icon', 'zone_new_companion\icon\icon.ico',
        '--add-data', 'zone_new_companion\icon\icon.ico;zone_new_companion/icon',
        '--add-data', 'media;media',
        '--add-data', 'zone_new_companion\ui\theme;zone_new_companion/ui/theme',
        '--hidden-import', 'mpv'
    )
    if ($NoLibmpv) {
        Write-Warning 'Building without libmpv: the built-in player is off, streams play in VLC.'
    } else {
        if (-not $Libmpv) { $Libmpv = Get-Libmpv }
        if (-not (Test-Path $Libmpv)) { throw "libmpv not found at $Libmpv" }
        # Bundled under the name python-mpv looks for; extracted to _MEIPASS at startup.
        $pyinstallerArgs += @('--add-binary', "$((Resolve-Path $Libmpv).Path);.")
    }
    $pyinstallerArgs += 'main.py'

    Write-Host 'Running PyInstaller...'
    Invoke-Checked $venvPython $pyinstallerArgs

    $exe = Join-Path $Root "dist\$Name.exe"
    if (-not (Test-Path $exe)) { throw "PyInstaller finished but $exe is missing." }
    $size = [math]::Round((Get-Item $exe).Length / 1MB)
    Write-Host ''
    Write-Host "Built $exe ($size MB)" -ForegroundColor Green
} finally {
    Pop-Location
}
