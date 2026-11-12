$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$archive = Join-Path $root 'VOIS_TransformSQL_Engine (1).zip.txt'
$project = Join-Path $root 'VOIS_TransformSQL_Engine'

if (-not (Test-Path -LiteralPath $archive)) {
    throw "Repository archive not found: $archive"
}

if (-not (Test-Path -LiteralPath (Join-Path $project 'backend\transformsql_engine.py'))) {
    $staging = Join-Path $root '.extract-transformsql'
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Path $staging | Out-Null
    $zip = Join-Path $staging 'project.zip'
    Copy-Item -LiteralPath $archive -Destination $zip
    Expand-Archive -LiteralPath $zip -DestinationPath $staging -Force
    $extracted = Join-Path $staging 'VOIS_TransformSQL_Engine'
    if (-not (Test-Path -LiteralPath (Join-Path $extracted 'backend\transformsql_engine.py'))) {
        throw 'The repository archive does not contain a valid TransformSQL project.'
    }
    if (Test-Path -LiteralPath $project) { Remove-Item -LiteralPath $project -Recurse -Force }
    Move-Item -LiteralPath $extracted -Destination $project
    Remove-Item -LiteralPath $staging -Recurse -Force
}

Push-Location $project
try {
    python backend\transformsql_engine.py --serve --port 8080
}
finally {
    Pop-Location
}
