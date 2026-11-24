[CmdletBinding()]
param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Message)

# Publishes reviewed workspace changes to the approved private GitHub repository.
#
# Order of work:
#   1. Check the remote, branch and merge state, then refresh origin/main.
#   2. Check the Git identity that will author the commit.
#   3. Stage everything into a temporary index and validate that index (paths and
#      file contents). The real index is only replaced after validation passes, so
#      a failed check leaves nothing staged.
#   4. Validate every local commit that origin/main does not have yet (paths,
#      contents and author/committer email addresses). Commits already on GitHub
#      were published earlier, so re-scanning them protects nothing. The new
#      commit's own identity was already checked in step 2, so every identity
#      check finishes before anything is committed.
#   5. Commit, push and confirm that GitHub main matches the local commit.
#
# A failed check in steps 1-4 leaves nothing staged, committed or pushed. A
# failure in step 5 (for example a rejected push) can leave the new local
# commit in place; nothing unchecked is pushed.
#
# Content scanning covers UTF-8 and UTF-16 text, the entries of ZIP-based files
# (.zip, .docx, .xlsx, .pptx), and a Latin-1 byte scan of other binaries up to
# 8 MB. Compressed archives it cannot open (gzip, 7z, rar, xz, bzip2, zstd, cab)
# and opaque binaries over 8 MB are refused unless reviewed by hand. Images and
# PDFs up to 8 MB are only byte-scanned: their compressed streams (PNG IDAT and
# zTXt, PDF Flate streams) are not decompressed.
#
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.

$ErrorActionPreference = 'Stop'
# PowerShell 7.3+ can turn native non-zero exits into errors. Exit codes are
# checked explicitly below (for example merge-base exit 1 means "not an ancestor").
$PSNativeCommandUseErrorActionPreference = $false

$expectedRemote = 'https://github.com/gonzothefreak-lgtm/bbc-efb.git'
$remoteBranchRef = 'refs/remotes/origin/main'

# Commit author and committer email addresses that may be published. Anything
# else must be a GitHub noreply address. GitHub's own web-commit identity
# (noreply@github.com) is allowed because it carries no personal address.
$allowedEmails = @('gonzothefreak-lgtm@users.noreply.github.com', 'noreply@github.com')
$allowedEmailPattern = '^([0-9]+\+)?[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?@users\.noreply\.github\.com$'

# Paths that must never be published. PowerShell -match is case-insensitive,
# which suits Windows file names.
$sensitivePath = '(^|/)(\.env(\.[^/]+)?|\.netrc|credentials\.json|secrets\.json|id_(rsa|ed25519)|__pycache__|\.venv|dist|build|\.concept-deps-[^/]+|core_live_readonly_[^/]+)(/|$)|\.(pem|key|pfx|p12|dll|exe|whl|log|pyc)$'

# Credential rules. Provider tokens are case-sensitive and must stand alone, so
# ordinary prose such as "risk-assessment-..." or "asiapacific..." does not match.
# The assignment rule is case-insensitive and ignores obvious placeholders.
$tokenPattern = '(?<![A-Za-z0-9_-])(sk-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{32,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{36,}|(AKIA|ASIA)[A-Z0-9]{16})(?![A-Za-z0-9_-])'
$privateKeyPattern = '-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----'
$assignmentPattern = '(?<![A-Za-z0-9])(api[_-]?key|password|client[_-]?secret|access[_-]?token)["'']?[ \t]*[:=][ \t]*["''](?![^"''\r\n]*(example|placeholder|changeme|redacted|your[_-]|xxxx))[A-Za-z0-9_+/=-]{20,}'

$culture = [System.Text.RegularExpressions.RegexOptions]::CultureInvariant
$ignoreCase = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
$secretRules = @(
    @{ Name = 'provider token'; Regex = (New-Object System.Text.RegularExpressions.Regex -ArgumentList $tokenPattern, $culture) },
    @{ Name = 'private key'; Regex = (New-Object System.Text.RegularExpressions.Regex -ArgumentList $privateKeyPattern, $culture) },
    @{ Name = 'credential assignment'; Regex = (New-Object System.Text.RegularExpressions.Regex -ArgumentList $assignmentPattern, ($culture -bor $ignoreCase)) }
)

# Size limits for content scanning.
$maxBlobBytes = 100MB            # any single file larger than this is refused
$maxOpaqueBinaryBytes = 8MB      # binaries that cannot be decoded (images etc.) up to this size are byte-scanned and allowed
$maxArchiveEntryBytes = 64MB     # one file inside a ZIP-based archive
$maxArchiveTotalBytes = 512MB    # all files inside one archive, against ZIP bombs
$maxArchiveDepth = 2             # archives inside archives

# Blob IDs of large or opaque binaries the owner has reviewed by hand. Add an
# entry as 'blob id' = 'path and reason' only after reviewing that exact file.
$reviewedBinaryBlobs = @{}

$script:scannedBlobs = @{}
$script:gitPath = $null

function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "Git failed while running $($args[0]) (exit $LASTEXITCODE). Review the Git output above." }
}

function Get-GitRecords {
    # Runs a Git command that was given -z and returns its NUL-separated records
    # as an array. -z output is never quoted, so non-ASCII paths stay intact.
    $output = @(& git -c core.quotepath=off @args)
    if ($LASTEXITCODE -ne 0) { throw "Git failed while running $($args[0]) (exit $LASTEXITCODE). Review the Git output above." }
    return @(($output -join "`n") -split "`0" | Where-Object { $_ -ne '' })
}

function Get-GitBlobBytes {
    param([Parameter(Mandatory)][string]$ObjectId)

    if ($ObjectId -notmatch '^[0-9a-f]{40}([0-9a-f]{24})?$') { throw "Unexpected Git object id '$ObjectId'." }
    if (-not $script:gitPath) { $script:gitPath = (Get-Command git -CommandType Application | Select-Object -First 1).Path }

    # PowerShell pipes decode native output as text, which corrupts binary data.
    # Read the blob through a raw process stream instead.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $script:gitPath
    $startInfo.Arguments = "cat-file blob $ObjectId"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $startInfo.WorkingDirectory = (Get-Location).ProviderPath
    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $errorText = $process.StandardError.ReadToEndAsync()
        $buffer = New-Object System.IO.MemoryStream
        $process.StandardOutput.BaseStream.CopyTo($buffer)
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "Git could not read object $ObjectId (exit $($process.ExitCode)): $($errorText.Result.Trim())" }
        # The leading comma stops PowerShell from unrolling the byte array.
        return , $buffer.ToArray()
    } finally {
        $process.Dispose()
    }
}

function Get-GitBlobSize {
    # Size in bytes from "git cat-file -s", read before the blob itself so an
    # oversized file is refused without loading it into memory.
    param([Parameter(Mandatory)][string]$ObjectId)

    if ($ObjectId -notmatch '^[0-9a-f]{40}([0-9a-f]{24})?$') { throw "Unexpected Git object id '$ObjectId'." }
    $size = @(& git cat-file -s $ObjectId)
    if ($LASTEXITCODE -ne 0 -or $size.Count -eq 0) { throw "Git could not read the size of object $ObjectId (exit $LASTEXITCODE)." }
    return [long]$size[0]
}

function Test-BytePrefix {
    param([byte[]]$Bytes, [byte[]]$Prefix)
    if ($Bytes.Length -lt $Prefix.Length) { return $false }
    for ($i = 0; $i -lt $Prefix.Length; $i++) { if ($Bytes[$i] -ne $Prefix[$i]) { return $false } }
    return $true
}

function Get-TextEncoding {
    # Returns the encoding to decode the bytes as text, or $null for binary data.
    param([byte[]]$Bytes)

    if (Test-BytePrefix $Bytes ([byte[]](0xFF, 0xFE))) { return [System.Text.Encoding]::Unicode }
    if (Test-BytePrefix $Bytes ([byte[]](0xFE, 0xFF))) { return [System.Text.Encoding]::BigEndianUnicode }
    if (Test-BytePrefix $Bytes ([byte[]](0xEF, 0xBB, 0xBF))) { return [System.Text.Encoding]::UTF8 }

    $sample = [Math]::Min($Bytes.Length, 8192)
    $evenNul = 0
    $oddNul = 0
    for ($i = 0; $i -lt $sample; $i++) {
        if ($Bytes[$i] -eq 0) { if ($i % 2 -eq 0) { $evenNul++ } else { $oddNul++ } }
    }
    if ($evenNul -eq 0 -and $oddNul -eq 0) { return [System.Text.Encoding]::UTF8 }
    $half = [Math]::Max(1, [Math]::Floor($sample / 2))
    # UTF-16 text without a byte-order mark: ASCII characters leave NUL bytes in
    # every other position.
    if ($oddNul / $half -gt 0.3 -and $evenNul / $half -lt 0.05) { return [System.Text.Encoding]::Unicode }
    if ($evenNul / $half -gt 0.3 -and $oddNul / $half -lt 0.05) { return [System.Text.Encoding]::BigEndianUnicode }
    return $null
}

function Find-Secrets {
    # Returns "location:line (rule)" for each credential-like match. The matched
    # text itself is never printed.
    param([string]$Text, [string]$Location)

    $found = New-Object System.Collections.Generic.List[string]
    foreach ($rule in $secretRules) {
        foreach ($match in $rule.Regex.Matches($Text)) {
            $line = 1 + ([regex]::Matches($Text.Substring(0, $match.Index), "`n")).Count
            $found.Add("${Location}:$line ($($rule.Name))")
            if ($found.Count -ge 20) { return , $found.ToArray() }
        }
    }
    return , $found.ToArray()
}

function Get-ContentProblems {
    # Scans one file's bytes. Text is decoded first (UTF-8, or UTF-16 with or
    # without a byte-order mark). ZIP-based archives (.zip, .docx, .xlsx, .pptx)
    # are opened and every entry name and entry body is checked. Compressed
    # formats that cannot be opened here, and large opaque binaries, fail closed.
    param([byte[]]$Bytes, [string]$Location, [string]$ObjectId, [int]$Depth = 0)

    $problems = New-Object System.Collections.Generic.List[string]
    if ($ObjectId -and $reviewedBinaryBlobs.ContainsKey($ObjectId)) { return , $problems.ToArray() }
    if ($Bytes.Length -gt $maxBlobBytes) {
        $problems.Add("${Location}: larger than $($maxBlobBytes / 1MB) MB and cannot be scanned")
        return , $problems.ToArray()
    }

    $isZip = (Test-BytePrefix $Bytes ([byte[]](0x50, 0x4B, 0x03, 0x04))) -or (Test-BytePrefix $Bytes ([byte[]](0x50, 0x4B, 0x05, 0x06)))
    if ($isZip) {
        if ($Depth -ge $maxArchiveDepth) {
            $problems.Add("${Location}: archive nested too deeply to scan")
            return , $problems.ToArray()
        }
        try {
            Add-Type -AssemblyName System.IO.Compression
            $stream = New-Object System.IO.MemoryStream -ArgumentList (, $Bytes)
            $archive = New-Object System.IO.Compression.ZipArchive -ArgumentList $stream, ([System.IO.Compression.ZipArchiveMode]::Read)
        } catch {
            $problems.Add("${Location}: looks like a ZIP archive but could not be opened")
            return , $problems.ToArray()
        }
        try {
            $total = 0
            foreach ($entry in $archive.Entries) {
                $entryName = $entry.FullName -replace '\\', '/'
                $entryLocation = "$Location!$entryName"
                if ($entryName -match $sensitivePath) { $problems.Add("${entryLocation}: sensitive or generated path inside an archive") }
                if ($entryName.EndsWith('/')) { continue }
                $total += $entry.Length
                if ($entry.Length -gt $maxArchiveEntryBytes -or $total -gt $maxArchiveTotalBytes) {
                    $problems.Add("${entryLocation}: archive contents too large to scan")
                    break
                }
                try {
                    $entryStream = $entry.Open()
                    try {
                        $entryBuffer = New-Object System.IO.MemoryStream
                        $entryStream.CopyTo($entryBuffer)
                    } finally {
                        $entryStream.Dispose()
                    }
                } catch {
                    $problems.Add("${entryLocation}: archive entry could not be read (it may be encrypted)")
                    continue
                }
                foreach ($problem in (Get-ContentProblems -Bytes ($entryBuffer.ToArray()) -Location $entryLocation -ObjectId '' -Depth ($Depth + 1))) { $problems.Add($problem) }
            }
        } finally {
            $archive.Dispose()
        }
        return , $problems.ToArray()
    }

    $opaqueArchive = @(
        [byte[]](0x1F, 0x8B),                         # gzip / .tgz
        [byte[]](0x37, 0x7A, 0xBC, 0xAF, 0x27, 0x1C), # 7z
        [byte[]](0x52, 0x61, 0x72, 0x21),             # rar
        [byte[]](0xFD, 0x37, 0x7A, 0x58, 0x5A),       # xz
        [byte[]](0x42, 0x5A, 0x68),                   # bzip2
        [byte[]](0x28, 0xB5, 0x2F, 0xFD),             # zstd
        [byte[]](0x4D, 0x53, 0x43, 0x46)              # cab
    )
    foreach ($magic in $opaqueArchive) {
        if (Test-BytePrefix $Bytes $magic) {
            $problems.Add("${Location}: compressed archive that this script cannot inspect; extract it or review it and add its blob id to `$reviewedBinaryBlobs")
            return , $problems.ToArray()
        }
    }

    $encoding = Get-TextEncoding $Bytes
    if ($null -eq $encoding) {
        if ($Bytes.Length -gt $maxOpaqueBinaryBytes) {
            $problems.Add("${Location}: binary file larger than $($maxOpaqueBinaryBytes / 1MB) MB that cannot be scanned; review it and add its blob id to `$reviewedBinaryBlobs")
            return , $problems.ToArray()
        }
        # Latin-1 maps every byte to one character, so ASCII tokens embedded in
        # an uncompressed binary are still found.
        $encoding = [System.Text.Encoding]::GetEncoding(28591)
    }
    foreach ($finding in (Find-Secrets -Text $encoding.GetString($Bytes) -Location $Location)) { $problems.Add("${finding}: possible credential") }
    return , $problems.ToArray()
}

function Get-PublishedEntries {
    # path<TAB>blob pairs already on GitHub main. Identical entries need no
    # second scan.
    $published = @{}
    & git rev-parse --verify --quiet "$remoteBranchRef^{commit}" | Out-Null
    if ($LASTEXITCODE -ne 0) { return $published }
    foreach ($record in (Get-GitRecords ls-tree -r -z $remoteBranchRef)) {
        $tab = $record.IndexOf("`t")
        $fields = $record.Substring(0, $tab) -split ' '
        $published["$($fields[2])`t$($record.Substring($tab + 1))"] = $true
    }
    return $published
}

function Assert-CleanEntries {
    # Entries are @{ Path; Blob; IsFile }. Throws with every problem found.
    param([object[]]$Entries, [hashtable]$Published, [string]$Label)

    $problems = New-Object System.Collections.Generic.List[string]
    foreach ($entry in $Entries) {
        if ($Published.ContainsKey("$($entry.Blob)`t$($entry.Path)")) { continue }
        if ($entry.Path -match $sensitivePath) { $problems.Add("$($entry.Path): sensitive or generated path") }
        if (-not $entry.IsFile -or $script:scannedBlobs.ContainsKey($entry.Blob)) { continue }
        $script:scannedBlobs[$entry.Blob] = $true
        if (-not $reviewedBinaryBlobs.ContainsKey($entry.Blob) -and (Get-GitBlobSize -ObjectId $entry.Blob) -gt $maxBlobBytes) {
            $problems.Add("$($entry.Path): larger than $($maxBlobBytes / 1MB) MB and cannot be scanned")
            continue
        }
        $bytes = Get-GitBlobBytes -ObjectId $entry.Blob
        foreach ($problem in (Get-ContentProblems -Bytes $bytes -Location $entry.Path -ObjectId $entry.Blob)) { $problems.Add($problem) }
    }
    if ($problems.Count -gt 0) {
        throw "Publishing stopped: $($problems.Count) problem(s) in ${Label}. Nothing was committed or pushed.`n  $($problems -join "`n  ")"
    }
}

function Get-IndexEntries {
    # ls-files -s -z records: "<mode> <blob> <stage><TAB><path>"
    foreach ($record in (Get-GitRecords ls-files -s -z)) {
        $tab = $record.IndexOf("`t")
        $fields = $record.Substring(0, $tab) -split ' '
        @{ Path = $record.Substring($tab + 1); Blob = $fields[1]; IsFile = ($fields[0] -ne '160000') }
    }
}

function Get-CommitEntries {
    # ls-tree -r -z records: "<mode> <type> <object><TAB><path>"
    param([string]$Commit)
    foreach ($record in (Get-GitRecords ls-tree -r -z $Commit)) {
        $tab = $record.IndexOf("`t")
        $fields = $record.Substring(0, $tab) -split ' '
        @{ Path = $record.Substring($tab + 1); Blob = $fields[2]; IsFile = ($fields[1] -eq 'blob') }
    }
}

function Test-AllowedEmail {
    param([string]$Email)
    return ($allowedEmails -contains $Email) -or ($Email -match $allowedEmailPattern)
}

function Get-IdentityEmail {
    # Uses git var so GIT_AUTHOR_EMAIL / GIT_COMMITTER_EMAIL overrides are seen too.
    param([string]$Variable)
    $ident = @(& git var $Variable)
    if ($LASTEXITCODE -ne 0 -or $ident.Count -eq 0) { return '' }
    if ($ident[0] -match '<([^>]*)>') { return $Matches[1] }
    return ''
}

function Get-UnpublishedCommits {
    & git rev-parse --verify --quiet "$remoteBranchRef^{commit}" | Out-Null
    if ($LASTEXITCODE -ne 0) { return @(Invoke-Git rev-list HEAD) }
    return @(Invoke-Git rev-list HEAD --not $remoteBranchRef)
}

function Assert-UnpublishedIdentities {
    $badCommits = New-Object System.Collections.Generic.List[string]
    foreach ($commit in (Get-UnpublishedCommits)) {
        $emails = @(& git log -1 --format='%ae%n%ce' $commit)
        if ($LASTEXITCODE -ne 0) { throw "Git failed while reading the identity of commit $commit." }
        if (@($emails | Where-Object { -not (Test-AllowedEmail $_) }).Count -gt 0) { $badCommits.Add($commit.Substring(0, 12)) }
    }
    if ($badCommits.Count -gt 0) {
        # The addresses are deliberately not printed, so they do not end up in logs.
        throw "Publishing stopped: $($badCommits.Count) unpublished commit(s) use an author or committer email that is not a GitHub noreply address: $($badCommits -join ', '). Set the noreply address with 'git config user.email <id>+<login>@users.noreply.github.com', then fix those commits locally (for the last commit: git commit --amend --reset-author --no-edit). Nothing was staged, committed or pushed."
    }
}

function Assert-RemoteIsAncestor {
    & git merge-base --is-ancestor $remoteBranchRef HEAD
    if ($LASTEXITCODE -eq 1) { throw 'GitHub main contains work that is missing locally. Review and integrate it before publishing.' }
    if ($LASTEXITCODE -ne 0) { throw "Could not compare local main with GitHub main (exit $LASTEXITCODE)." }
}

Push-Location -LiteralPath $PSScriptRoot
$checkIndex = $null
$messageFile = $null
try {
    if ((Invoke-Git remote get-url --push origin) -cne $expectedRemote) { throw 'Origin does not match the approved private GitHub repository.' }
    if ((Invoke-Git branch --show-current) -cne 'main') { throw 'Switch to main before publishing.' }
    if (@(Invoke-Git ls-files --unmerged).Count -gt 0) { throw 'Resolve the existing merge conflicts before publishing.' }

    # Refresh origin/main first so "unpublished commits" is measured against
    # GitHub's current state. A stale ref would only make the checked range larger.
    Invoke-Git -c credential.interactive=false fetch origin "+refs/heads/main:$remoteBranchRef"
    Assert-RemoteIsAncestor

    foreach ($variable in 'GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT') {
        if (-not (Test-AllowedEmail (Get-IdentityEmail $variable))) {
            throw "Publishing stopped: the Git identity on this computer ($variable) does not use a GitHub noreply email address. Run 'git config user.email <id>+<login>@users.noreply.github.com' in this repository and try again. Nothing was staged."
        }
    }

    $published = Get-PublishedEntries

    # Stage into a temporary copy of the index and validate it there.
    $realIndex = [System.IO.Path]::Combine((Get-Location).ProviderPath, (Invoke-Git rev-parse --git-path index))
    $checkIndex = "$realIndex.publish-check"
    if (Test-Path -LiteralPath $realIndex) { Copy-Item -LiteralPath $realIndex -Destination $checkIndex -Force }
    $env:GIT_INDEX_FILE = $checkIndex
    Invoke-Git add --all
    Assert-CleanEntries -Entries @(Get-IndexEntries) -Published $published -Label 'the files to be committed'
    Remove-Item Env:GIT_INDEX_FILE

    foreach ($commit in (Get-UnpublishedCommits)) {
        Assert-CleanEntries -Entries @(Get-CommitEntries -Commit $commit) -Published $published -Label "unpublished commit $commit"
    }
    # Existing local commits are checked before the new commit is made, so a bad
    # identity on an older commit leaves no new commit behind.
    Assert-UnpublishedIdentities

    # Validation passed: make the checked index the real one, then commit.
    Move-Item -LiteralPath $checkIndex -Destination $realIndex -Force
    $checkIndex = $null
    if (@(Invoke-Git diff --cached --name-only).Count -gt 0) {
        # A message file avoids Windows PowerShell 5.1's quoting of native arguments.
        $messageFile = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($messageFile, $Message, (New-Object System.Text.UTF8Encoding -ArgumentList $false))
        Invoke-Git commit --file $messageFile
    }

    Assert-RemoteIsAncestor
    Invoke-Git -c credential.interactive=false push --set-upstream origin main

    $localCommit = Invoke-Git rev-parse HEAD
    $remoteHead = Invoke-Git -c credential.interactive=false ls-remote --heads origin refs/heads/main
    if (($remoteHead -split "`t")[0] -cne $localCommit) { throw 'Could not confirm that GitHub main matches the published commit.' }
    Write-Output "Published $localCommit to $expectedRemote"
} finally {
    Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    if ($checkIndex -and (Test-Path -LiteralPath $checkIndex)) { Remove-Item -LiteralPath $checkIndex -Force -ErrorAction SilentlyContinue }
    if ($messageFile -and (Test-Path -LiteralPath $messageFile)) { Remove-Item -LiteralPath $messageFile -Force -ErrorAction SilentlyContinue }
    Pop-Location
}
