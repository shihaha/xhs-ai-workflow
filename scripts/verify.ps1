[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Checked {
    param([string]$Label, [scriptblock]$Action)
    Write-Host "`n[$Label]"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

Push-Location $repoRoot
try {
    $pythonVersion = python -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required." }
    $nodeVersion = node -p "process.versions.node"
    node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>20 || (a===20 && b>=19) ? 0 : 1)"
    if ($LASTEXITCODE -ne 0) { throw "Node 20.19+ is required." }
    Write-Host "Python $pythonVersion; Node $nodeVersion"

    Invoke-Checked "Backend tests" { python -m pytest -q }
    Invoke-Checked "Python compile" { python -m compileall -q backend tools frontend/e2e/fixture_app.py }

    Push-Location (Join-Path $repoRoot "frontend")
    try {
        Invoke-Checked "Frontend tests" { npm test -- --run }
        Invoke-Checked "Frontend build" { npm run build }
        Invoke-Checked "Controlled fresh-runtime E2E" { npm run test:e2e }
        Invoke-Checked "Frontend dependency audit" { npm audit --audit-level=high }
    } finally {
        Pop-Location
    }

    Write-Host "`n[Tracked-file secret scan]"
    $matches = git grep -n -I -E "(sk-[A-Za-z0-9_-]{20,}|Bearer[[:space:]]+[A-Za-z0-9_-]{30,})" -- .
    if ($LASTEXITCODE -eq 0) {
        throw "Potential secret-shaped value found in a tracked file."
    }
    if ($LASTEXITCODE -ne 1) {
        throw "Secret scan could not complete."
    }

    Invoke-Checked "Boundary source scan" {
        python tools/scan_release_boundaries.py --root $repoRoot
    }

    Write-Host "`nAll controlled software verification gates passed. Live gates remain separate in docs/UAT_CHECKLIST.md."
} finally {
    Pop-Location
}
