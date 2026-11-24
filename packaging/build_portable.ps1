[CmdletBinding()]
param(
    # Python version for the Windows "py" launcher, used when -PythonExe is not given.
    [ValidatePattern('^3\.[0-9]+$')][string]$PythonVersion = '3.12',
    # Full path to a 64-bit python.exe to build with, instead of the "py" launcher.
    [string]$PythonExe,
    # Changelog to ship. Default: CHANGELOG.md in the repository root.
    [string]$Changelog,
    # Folder of pre-downloaded wheels for an offline install (pip --no-index --find-links).
    [string]$WheelDir,
    # Keep the existing build virtual environment instead of recreating it.
    [switch]$ReuseVenv,
    # Build even though the Git working tree has uncommitted changes (recorded in TEST_STATUS.txt).
    [switch]$AllowDirty,
    # Leave the simulated provider (bbc_core.simulated) out of the frozen app.
    [switch]$ExcludeSimulated,
    # Start the frozen BBC-EFB.exe once and check it stays running.
    [switch]$LaunchCheck,
    [ValidateRange(3, 120)][int]$LaunchCheckSeconds = 10
)

# Builds the BBC EFB portable Windows delivery. See packaging/README.md.
#
# Order of work (any failure stops the build and nothing is zipped):
#   1. Check this is Windows, the Python interpreter, the entry point, the
#      version resource, the changelog and the Git state.
#   2. Create a fresh virtual environment in build\package-venv and install the
#      pinned packages in packaging\requirements-build.txt (wheels only, no
#      dependency resolution), then run "pip check".
#   3. Run the guarded offline test suite with that environment. On failure,
#      write build\package-work\TEST_STATUS.txt and refuse to package.
#   4. Run PyInstaller with packaging\bbc-efb.spec (onedir, windowed).
#   5. Optionally start the frozen app once (-LaunchCheck).
#   6. Write the portable ZIP and the source ZIP, verify both (no tests,
#      work-in-progress tests, logs, probe outputs, bytecode caches, secrets or
#      SimConnect.dll), and write SHA256SUMS.txt.
#
# Output: dist\BBC-EFB-<version>\ with
#   BBC-EFB-<version>-win64-portable.zip, BBC-EFB-<version>-source.zip,
#   TEST_STATUS.txt and SHA256SUMS.txt.
#
# The script downloads the pinned packages from PyPI unless -WheelDir is given.
# It never contacts the simulator and never copies SimConnect.dll.
#
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
Set-StrictMode -Version 2.0

$packagingDir = $PSScriptRoot
$root = Split-Path -Parent $packagingDir
$buildDir = [System.IO.Path]::Combine($root, 'build')
$venvDir = [System.IO.Path]::Combine($buildDir, 'package-venv')
$venvPython = [System.IO.Path]::Combine($venvDir, 'Scripts', 'python.exe')
$workDir = [System.IO.Path]::Combine($buildDir, 'package-work')
$pyiWorkDir = [System.IO.Path]::Combine($buildDir, 'pyinstaller-work')
$pyiDistDir = [System.IO.Path]::Combine($buildDir, 'pyinstaller-dist')
$reportDir = [System.IO.Path]::Combine($buildDir, 'pyinstaller-report')
$appDir = [System.IO.Path]::Combine($pyiDistDir, 'BBC-EFB')
$exePath = [System.IO.Path]::Combine($appDir, 'BBC-EFB.exe')
$tools = [System.IO.Path]::Combine($packagingDir, 'package_tools.py')
$spec = [System.IO.Path]::Combine($packagingDir, 'bbc-efb.spec')
$requirements = [System.IO.Path]::Combine($packagingDir, 'requirements-build.txt')
$portableReadme = [System.IO.Path]::Combine($packagingDir, 'PORTABLE_README.md')
$limitations = [System.IO.Path]::Combine($packagingDir, 'LIMITATIONS.md')
$notices = [System.IO.Path]::Combine($packagingDir, 'THIRD_PARTY_NOTICES.md')
$testOutput = [System.IO.Path]::Combine($workDir, 'TEST_OUTPUT.txt')
$testStatus = [System.IO.Path]::Combine($workDir, 'TEST_STATUS.txt')
$modulesReport = [System.IO.Path]::Combine($reportDir, 'BUNDLED_MODULES.txt')

function Write-Step {
    param([Parameter(Mandatory)][string]$Text)
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Stop-Build {
    param([Parameter(Mandatory)][string]$Reason)
    throw [System.InvalidOperationException]::new($Reason)
}

function Invoke-Native {
    # Runs a native command with an argument array and stops the build on a
    # non-zero exit code. Output goes straight to the console.
    param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    & $Exe @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) { Stop-Build "$What failed (exit code $code). Review the output above." }
}

function Invoke-Tool {
    # Runs a packaging/package_tools.py command with the build environment's Python.
    param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][string[]]$Arguments)
    Invoke-Native -What $What -Exe $venvPython -Arguments (@('-I', $tools) + $Arguments + @('--root', $root))
}

function Remove-BuildFolder {
    # Removes a folder this script owns. Refuses anything outside build\ or dist\.
    param([Parameter(Mandatory)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $allowed = @([System.IO.Path]::GetFullPath($buildDir), [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($root, 'dist')))
    $inside = $false
    foreach ($parent in $allowed) {
        if ($full.StartsWith($parent + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) { $inside = $true }
    }
    if (-not $inside) { Stop-Build "Refusing to remove $full because it is outside build\ and dist\." }
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

try {
    Write-Step 'Checking the build machine and sources'
    if ($env:OS -ne 'Windows_NT') { Stop-Build 'The portable build runs on Windows 10/11 x64 only.' }
    if (-not [System.Environment]::Is64BitOperatingSystem) { Stop-Build 'A 64-bit Windows is required.' }
    foreach ($required in @($tools, $spec, $requirements, $portableReadme, $limitations, $notices,
                            [System.IO.Path]::Combine($packagingDir, 'version_info.txt'),
                            [System.IO.Path]::Combine($packagingDir, 'launch_bbc_efb.py'),
                            [System.IO.Path]::Combine($root, 'tests', 'test_repo_offline_guard.py'))) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { Stop-Build "Required file is missing: $required" }
    }

    if ($PythonExe) {
        if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { Stop-Build "Python not found at $PythonExe" }
        $baseExe = $PythonExe
        $baseArgs = @()
    } else {
        if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
            Stop-Build 'The Windows "py" launcher was not found. Install 64-bit Python from python.org or pass -PythonExe.'
        }
        $baseExe = 'py'
        $baseArgs = @("-$PythonVersion")
    }
    Invoke-Native -What 'The Python interpreter check' -Exe $baseExe -Arguments ($baseArgs + @('-I', $tools, 'check-interpreter'))

    $changelogPath = $Changelog
    if (-not $changelogPath) { $changelogPath = [System.IO.Path]::Combine($root, 'CHANGELOG.md') }
    $changelogPath = [System.IO.Path]::GetFullPath($changelogPath)

    $git = Get-Command git -ErrorAction SilentlyContinue
    $gitDirty = $false
    if ($git) {
        # Windows PowerShell 5.1 turns redirected native stderr into a terminating
        # error while ErrorActionPreference is Stop, so relax it for this call.
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $porcelain = @(& git -C $root status --porcelain --untracked-files=normal 2>$null)
            $gitExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($gitExit -eq 0) {
            $gitDirty = ($porcelain.Count -gt 0)
        } else {
            Write-Host 'Note: this folder is not a Git working tree; TEST_STATUS.txt will record the commit as unknown.'
        }
    } else {
        Write-Host 'Note: Git was not found; TEST_STATUS.txt will record the commit as unknown.'
    }
    if ($gitDirty -and -not $AllowDirty) {
        Stop-Build 'The Git working tree has uncommitted changes. Commit them first so the delivery matches a commit, or pass -AllowDirty (recorded in TEST_STATUS.txt).'
    }

    Write-Step 'Creating the build environment'
    if (-not $ReuseVenv -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Remove-BuildFolder $venvDir
        Invoke-Native -What 'Creating the virtual environment' -Exe $baseExe -Arguments ($baseArgs + @('-m', 'venv', $venvDir))
    }
    New-Item -ItemType Directory -Force -Path $workDir | Out-Null
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-deps',
                 '--only-binary=:all:', '--requirement', $requirements)
    if ($WheelDir) { $pipArgs += @('--no-index', '--find-links', $WheelDir) }
    # pip is run from the work folder so the repository's "packaging" folder can
    # never shadow the PyPI "packaging" module.
    Push-Location $workDir
    try {
        Invoke-Native -What 'Installing the pinned build dependencies' -Exe $venvPython -Arguments $pipArgs
        Invoke-Native -What 'Checking the installed dependencies (pip check)' -Exe $venvPython -Arguments @('-m', 'pip', 'check', '--disable-pip-version-check')
    } finally {
        Pop-Location
    }

    $version = (& $venvPython -I $tools version --root $root | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $version) { Stop-Build 'Could not read bbc_efb.VERSION.' }
    $version = $version.Trim()
    $label = $version -replace '[^0-9A-Za-z.+-]', '-'
    Write-Host "Application version: $version"
    Invoke-Tool -What 'The entry point check' -Arguments @('check-entry-point')
    Invoke-Tool -What 'The version resource check' -Arguments @('check-version-info')
    Invoke-Tool -What 'The changelog check' -Arguments @('check-changelog', $changelogPath)

    Write-Step 'Running the guarded offline test suite'
    Remove-Item -LiteralPath $testOutput, $testStatus -Force -ErrorAction SilentlyContinue
    & $venvPython -I $tools run-tests --root $root --python $venvPython --output $testOutput
    $testExit = $LASTEXITCODE
    & $venvPython -I $tools test-status --root $root --test-output $testOutput --exit-code $testExit --pip-freeze --launch-check 'not run' --out $testStatus
    $statusExit = $LASTEXITCODE
    if ($testExit -ne 0 -or $statusExit -ne 0) {
        Stop-Build "The offline tests did not pass, so nothing was packaged. See $testStatus and $testOutput."
    }

    Write-Step 'Freezing the EFB with PyInstaller'
    Remove-BuildFolder $pyiWorkDir
    Remove-BuildFolder $pyiDistDir
    Remove-BuildFolder $reportDir
    $previousReport = $env:BBC_EFB_REPORT_DIR
    $previousExclude = $env:BBC_EFB_EXCLUDE_SIMULATED
    $previousBytecode = $env:PYTHONDONTWRITEBYTECODE
    $env:BBC_EFB_REPORT_DIR = $reportDir
    $env:BBC_EFB_EXCLUDE_SIMULATED = $(if ($ExcludeSimulated) { '1' } else { '' })
    $env:PYTHONDONTWRITEBYTECODE = '1'
    Push-Location $workDir
    try {
        Invoke-Native -What 'PyInstaller' -Exe $venvPython -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean',
            '--log-level', 'WARN', '--distpath', $pyiDistDir, '--workpath', $pyiWorkDir, $spec)
    } finally {
        Pop-Location
        $env:BBC_EFB_REPORT_DIR = $previousReport
        $env:BBC_EFB_EXCLUDE_SIMULATED = $previousExclude
        $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
    }
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) { Stop-Build "PyInstaller finished but $exePath is missing." }
    if (-not (Test-Path -LiteralPath $modulesReport -PathType Leaf)) { Stop-Build "The spec did not write $modulesReport." }

    $launchText = 'not run (pass -LaunchCheck to start the frozen app once)'
    if ($LaunchCheck) {
        Write-Step "Starting BBC-EFB.exe for $LaunchCheckSeconds seconds"
        $process = Start-Process -FilePath $exePath -WorkingDirectory $appDir -PassThru
        Start-Sleep -Seconds $LaunchCheckSeconds
        if ($process.HasExited) {
            Stop-Build "BBC-EFB.exe exited within $LaunchCheckSeconds s (exit code $($process.ExitCode)). Check the log in %LOCALAPPDATA%\BBC EFB\logs. Nothing was packaged."
        }
        Stop-Process -Id $process.Id -Force
        $launchText = "BBC-EFB.exe stayed running for $LaunchCheckSeconds s and was then stopped; the window content was not inspected"
    }
    Invoke-Tool -What 'Writing TEST_STATUS.txt' -Arguments @('test-status', '--test-output', $testOutput,
        '--exit-code', "$testExit", '--pip-freeze', '--launch-check', $launchText, '--out', $testStatus)

    Write-Step 'Writing and verifying the delivery ZIPs'
    $deliveryDir = [System.IO.Path]::Combine($root, 'dist', "BBC-EFB-$label")
    Remove-BuildFolder $deliveryDir
    New-Item -ItemType Directory -Force -Path $deliveryDir | Out-Null
    $portableZip = [System.IO.Path]::Combine($deliveryDir, "BBC-EFB-$label-win64-portable.zip")
    $sourceZip = [System.IO.Path]::Combine($deliveryDir, "BBC-EFB-$label-source.zip")
    try {
        Invoke-Tool -What 'Writing the portable ZIP' -Arguments @('assemble-portable', '--app-dir', $appDir,
            '--out', $portableZip, '--readme', $portableReadme, '--limitations', $limitations,
            '--changelog', $changelogPath, '--test-status', $testStatus, '--test-output', $testOutput,
            '--notices', $notices, '--modules', $modulesReport)
        Invoke-Tool -What 'Verifying the portable ZIP' -Arguments @('verify-portable', $portableZip)
        Invoke-Tool -What 'Writing the source ZIP' -Arguments @('source-zip', '--out', $sourceZip)
        Invoke-Tool -What 'Verifying the source ZIP' -Arguments @('verify-source', $sourceZip)
        Copy-Item -LiteralPath $testStatus -Destination ([System.IO.Path]::Combine($deliveryDir, 'TEST_STATUS.txt'))
        Invoke-Native -What 'Writing SHA256SUMS.txt' -Exe $venvPython -Arguments @('-I', $tools, 'sha256sums',
            '--out', ([System.IO.Path]::Combine($deliveryDir, 'SHA256SUMS.txt')), $portableZip, $sourceZip,
            ([System.IO.Path]::Combine($deliveryDir, 'TEST_STATUS.txt')))
    } catch {
        # Never leave a ZIP behind that failed verification.
        Remove-BuildFolder $deliveryDir
        throw
    }

    Write-Step 'Done'
    Write-Host "Delivery folder: $deliveryDir"
    Get-ChildItem -LiteralPath $deliveryDir | ForEach-Object { Write-Host ("  {0}  ({1:N0} bytes)" -f $_.Name, $_.Length) }
    Write-Host 'Live MSFS behaviour is NOT verified by this build. See LIMITATIONS.md before sharing it.'
    exit 0
} catch {
    Write-Host ''
    Write-Host "Build stopped: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
