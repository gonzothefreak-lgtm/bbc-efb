[CmdletBinding()]
param([Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Message)

$ErrorActionPreference = 'Stop'
$expectedRemote = 'https://github.com/gonzothefreak-lgtm/bbc-efb.git'
$secretPattern = '-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{32,}|(AKIA|ASIA)[A-Z0-9]{16}|(api[_-]?key|password|client[_-]?secret|access[_-]?token)[[:space:]]*[:=][[:space:]]*["''][A-Za-z0-9_+/=-]{20,}'
$sensitivePath = '(^|/)(\.env(\.[^/]+)?|\.netrc|credentials\.json|secrets\.json|id_(rsa|ed25519)|__pycache__|\.venv|dist|build|\.concept-deps-[^/]+|core_live_readonly_[^/]+)(/|$)|\.(pem|key|pfx|p12|dll|exe|whl|log|pyc)$'

function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { throw "Git failed while running $($args[0]) (exit $LASTEXITCODE). Review the Git output above." }
}

function Assert-NoSecrets {
    param([string[]]$RevisionArgs, [string]$Label)

    $matchedPaths = & git grep -I -l -i -E -e $secretPattern @RevisionArgs --
    if ($LASTEXITCODE -eq 0) { throw "Possible credential in $Label. Review these paths locally: $($matchedPaths -join ', ')" }
    if ($LASTEXITCODE -ne 1) { throw "Credential check failed for $Label (exit $LASTEXITCODE)." }
}

Push-Location -LiteralPath $PSScriptRoot
try {
    if ((Invoke-Git remote get-url --push origin) -cne $expectedRemote) { throw 'Origin does not match the approved private GitHub repository.' }
    if ((Invoke-Git branch --show-current) -cne 'main') { throw 'Switch to main before publishing.' }
    if (Invoke-Git ls-files --unmerged) { throw 'Resolve the existing merge conflicts before publishing.' }

    Invoke-Git add --all
    $stagedPaths = Invoke-Git ls-files --cached
    $blocked = $stagedPaths -match $sensitivePath
    if ($blocked) { throw "Sensitive or generated path in the Git index: $($blocked -join ', ')" }
    Assert-NoSecrets -RevisionArgs @('--cached') -Label 'the Git index'
    foreach ($commit in (Invoke-Git rev-list HEAD)) {
        $blocked = (Invoke-Git ls-tree -r --name-only $commit) -match $sensitivePath
        if ($blocked) { throw "Sensitive or generated path in commit ${commit}: $($blocked -join ', ')" }
        Assert-NoSecrets -RevisionArgs @($commit) -Label "commit $commit"
    }

    if (Invoke-Git diff --cached --name-only) { Invoke-Git commit -m $Message }
    Invoke-Git -c credential.interactive=false fetch origin main
    & git merge-base --is-ancestor FETCH_HEAD HEAD
    if ($LASTEXITCODE -ne 0) { throw 'GitHub main contains work that is missing locally. Review and integrate it before publishing.' }
    Invoke-Git -c credential.interactive=false push --set-upstream origin main

    $localCommit = Invoke-Git rev-parse HEAD
    $published = Invoke-Git -c credential.interactive=false ls-remote --heads origin refs/heads/main
    if (($published -split "`t")[0] -cne $localCommit) { throw 'Could not confirm that GitHub main matches the published commit.' }
    Write-Output "Published $localCommit to $expectedRemote"
} finally {
    Pop-Location
}
