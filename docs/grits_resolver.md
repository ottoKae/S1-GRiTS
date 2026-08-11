# GRiTS-Resolver integration

S1-GRiTS remains a fact-data producer. Cross-sensor reads are provided by the independently versioned public package at `https://github.com/ottoKae/GRiTS-Resolver`.

Install the pinned release:

```powershell
.\tools\install_grits_resolver.ps1 -Version v0.1.0
```

For local development:

```powershell
.\tools\install_grits_resolver.ps1 -LocalPath ..\GRiTS-Resolver
```

The installer always runs `verify_grits_resolver.py`, which checks the installed version, GDAL data discovery, and the shared optical alias contract.
