param(
    [string]$Version = "v0.1.0",
    [string]$LocalPath = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if ($LocalPath) {
    $resolved = (Resolve-Path -LiteralPath $LocalPath).Path
    & $Python -m pip install -e $resolved --no-build-isolation
} else {
    $requirement = "grits-resolver @ git+https://github.com/ottoKae/GRiTS-Resolver.git@$Version"
    & $Python -m pip install --upgrade $requirement --no-build-isolation
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python "$PSScriptRoot\verify_grits_resolver.py" --expected-version $Version.TrimStart("v")
exit $LASTEXITCODE
