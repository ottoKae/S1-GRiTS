# S1-GRiTS Release Process

This document is the maintainer contract for publishing the Python package,
GitHub Release and bundled Chinese web console. A release is immutable: never
reuse an existing PyPI version or move a published tag.

## 1. What is released

One tag publishes two artifacts:

- `s1grits-X.Y.Z-py3-none-any.whl`
- `s1grits-X.Y.Z.tar.gz`

The wheel is pure Python but depends on binary geospatial wheels. Supported
Python is currently `>=3.12,<3.13`. The wheel includes the three spatial
GeoParquet dictionaries and the single canonical v3 Chinese frontend under
`s1grits/webapp/static/`.

The retired Streamlit/English prototype is not a second product: it is absent
from `src/`, package-data rules and the wheel. Future English support should be
implemented by internationalising the same static SPA, not by restoring a
parallel `webapp_en` tree.

## 2. Version and tag contract

The following values must agree exactly:

- `[project].version` in `pyproject.toml`;
- `__version__` in `src/s1grits/__version__.py`;
- the Git tag, with a leading `v`.

Stable tags use `vX.Y.Z`; release candidates use `vX.Y.Z-rcN`, which maps to
the PEP 440 package version `X.Y.ZrcN`. Feature additions such as the Chinese
GUI and 10 m optimized resampling require a minor release (for example
`3.1.0`), not reuse of the already published `3.0.0`.

## 3. Prepare the release commit

Work on a feature branch, then:

1. Finish and review `CHANGELOG.md` under `Unreleased`.
2. Set the next version in both version files.
3. Run the offline source gate.
4. Run lint, tests and a local package build.
5. Merge the reviewed commit to `main` and push `main` before tagging.

Windows/PowerShell example for a future `v3.1.0` release:

```powershell
python tools/release_check.py --candidate-tag v3.1.0
python -m ruff check src tests tools benchmarks
python -m mypy
python -m pytest tests -q --tb=short
python -m build
python -m twine check --strict dist/*
```

The candidate gate is network-free. It verifies version parity, tag syntax,
required GeoParquet and web assets, the `zh-CN` canonical page, absence of a
tracked legacy frontend, package-data rules and wheel-first quick-start docs.
It also rejects tracked email addresses, user-home paths, high-confidence
private keys and common GitHub/AWS token formats.

### Commit identity and private operational files

Before making the release commit, enable **Keep my email addresses private**
in GitHub and copy the exact no-reply address that GitHub provides. Configure
it for this repository only:

```powershell
git config user.email "<your GitHub-provided no-reply address>"
git config --get user.email
```

Do not guess the numeric prefix of the no-reply address. The strict release
gate requires the release commit author to use a `users.noreply.github.com`
address. One-off production configs, recovery scripts, workstation paths,
remote hosts and account names must remain outside tracked source. The
repository `.gitignore` protects the current Ecuador production-operation
files, but maintainers must still review `git status` before every commit.

The source gate does not rewrite old commits. If an ordinary contact email or
local path already exists in published history, remove it from current source
and fix forward. Rewrite shared history only after a separate impact review;
if a real credential was exposed, revoke or rotate it immediately regardless
of whether history is later rewritten.

## 4. Create and publish the tag

After the release commit is present on `origin/main`:

```powershell
git switch main
git pull --ff-only origin main
git tag -a v3.1.0 -m "S1-GRiTS 3.1.0"
python tools/release_check.py --release-tag v3.1.0
git push origin v3.1.0
```

The strict gate additionally requires a completely clean worktree, an
annotated tag pointing to `HEAD`, a commit reachable from `origin/main`, and a
GitHub no-reply author email on the release commit.
Pushing the tag triggers `.github/workflows/build_wheels.yml`, which runs the
network-free test suite on Linux, macOS and Windows, builds the universal wheel
and sdist, checks them with Twine, creates the GitHub Release and publishes to
PyPI using Trusted Publishing.

Do not run `twine upload` manually for a normal release.

## 5. Post-publication verification

Use a new Python 3.12 environment so an editable checkout cannot mask a broken
wheel:

```powershell
python -m venv .venv-release-smoke
.\.venv-release-smoke\Scripts\python -m pip install --upgrade pip
.\.venv-release-smoke\Scripts\python -m pip install "s1grits[web]==3.1.0"
.\.venv-release-smoke\Scripts\s1grits --version
.\.venv-release-smoke\Scripts\s1grits init smoke.yaml
.\.venv-release-smoke\Scripts\s1grits doctor --config smoke.yaml
.\.venv-release-smoke\Scripts\s1grits serve smoke-output --port 5556
```

Confirm that `http://127.0.0.1:5556/` opens the Chinese console and that
`/api/capabilities` reports the released version. Also confirm both artifacts
appear on GitHub Releases and PyPI.

If a published build is defective, stop recommending it, yank that exact PyPI
version when appropriate, fix forward with a new patch version, and retain the
old immutable tag for auditability.
