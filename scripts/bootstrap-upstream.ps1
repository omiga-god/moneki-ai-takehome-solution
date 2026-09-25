param(
    [string]$SourceDir = "",
    [string]$RepoUrl = "https://github.com/MorrisPRC/moneki-ai-takehome.git"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = $SourceDir

if (-not $source) {
    $workDir = Join-Path $projectRoot "work"
    New-Item -ItemType Directory -Force -Path $workDir | Out-Null
    $source = Join-Path $workDir "upstream"
    if (-not (Test-Path -LiteralPath $source)) {
        git clone --depth 1 $RepoUrl $source
        if ($LASTEXITCODE -ne 0) { throw "Git clone failed. Download the official ZIP and retry with -SourceDir." }
    }
}

$source = (Resolve-Path -LiteralPath $source).Path
$required = @("README.md", "data", "knowledge_base", "starter", "eval", "docs")
foreach ($item in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $item))) {
        throw "Missing official repository item: $item in $source"
    }
}

foreach ($item in @("data", "knowledge_base", "starter", "eval", "docs")) {
    $destination = Join-Path $projectRoot $item
    if (Test-Path -LiteralPath $destination) {
        throw "$item already exists. Import only once; inspect existing files before replacing anything."
    }
}

foreach ($item in @("data", "knowledge_base", "starter", "eval", "docs")) {
    $destination = Join-Path $projectRoot $item
    Copy-Item -LiteralPath (Join-Path $source $item) -Destination $destination -Recurse
}

Copy-Item -LiteralPath (Join-Path $source "README.md") -Destination (Join-Path $projectRoot "UPSTREAM_README.md")
Write-Host "Official files imported. Read UPSTREAM_README.md and IMPLEMENTATION_GUIDE.md next."
