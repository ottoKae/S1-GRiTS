# S1-GRiTS Release Process

This document is the maintainer contract for publishing the Python package,
GitHub Release and bundled existing Chinese Web interface. A release is immutable: never
reuse an existing PyPI version or move a published tag.

<a id="v4-scope"></a>
## V4.0.0 candidate scope / 候选发布范围

V4.0.0 is the target, not the currently published version or a completed release
gate. The local package version is prepared as 4.0.0. The [changelog](../CHANGELOG.md),
[migration guide](migration_v4.md) and [entry/support matrix](entry_points.md)
describe the deliverable behavior.

| Scope / 范围 | Release treatment / 发布处理 |
| --- | --- |
| Existing production and correctness fixes / 生产与修复 | Preserve defaults; include exact 30 m nearest-neighbour mapping, NoData/count/empty-month fixes and compatible resume checks. Historical products remain unchanged. |
| Explicit optional tools / 显式可选工具 | Include intra-pass strategies, filtering/features, monthly mean/median, interpolation, product/group access, registration, views/materialization, multi-track monthly combination and inter-tile mosaics, within their documented input contracts. |
| User entry points / 使用入口 | Documented YAML, CLI, Python and generic Agent routes. Skills are maintained separately from the wheel. Retain the existing Chinese Web; new Web tools, bilingual switching and Web fixes are deferred to `feature/web-console-v4` for a later release. |
| Local LandTrendr / 本地 LandTrendr | Experimental, separately installed backend; point, point-set and small-window scope. Do not claim GEE equivalence or SAR detection accuracy. |
| Public CUSUM / 公共 CUSUM | Ship the independent experimental adapter, tests and instructions; user installs nrt separately. No bundled/modified nrt, automatic extra or Web/SaaS delivery. The [decision](local_backends.md#cusum-distribution-decision) records the inspected scope and limits. |
| GEE segmentation / GEE 分段 | Explicit optional single-point route; no automatic fallback. Offline checks do not establish online acceptance. |
| Local CCDC / 本地 CCDC | Not implemented and excluded from supported V4 capabilities. It may continue as a separate follow-up without being promised by this release. |
| Research / 研究材料 | Manuscripts, research datasets, private operational files and domain-specific skills do not enter Python distributions. |

本轮交付以已验收的有限扩展为范围，不要求实现本地 CCDC 才能定义 V4；
但不能把它列为正式支持。CUSUM 本次分发范围已明确为适配器及说明，后端由用户另装；
重新分发后端、商业组合交付和 Web／SaaS 不在此决定范围。
严格 STAC 全模式／扩展合规、大洲吞吐和跨后端科学等价均不作为已完成能力。
软件包包含公开说明及所需参考字典，不包含用户论文和研究数据。

## 1. What is released

One tag publishes two artifacts:

- `s1grits-X.Y.Z-py3-none-any.whl`
- `s1grits-X.Y.Z.tar.gz`

The wheel is pure Python but depends on binary geospatial wheels. Supported
Python is currently `>=3.12,<3.13`. The wheel includes the three spatial
GeoParquet dictionaries and the retained Chinese frontend under
`s1grits/webapp/static/`.

The source distribution also includes the generated Cube/analysis skills and
their version/hash manifest under `agents/skills/`, plus the existing handoff
reference. They are not installed by the wheel. Regenerate from the sole editable
source when skills or the package version change; see the [skill export guide](../agents/skills/README.md).

The retired Streamlit/English prototype is not a second product: it is absent
from `src/`, package-data rules and the wheel. The bilingual console remains on
`feature/web-console-v4`; it is not part of this candidate's Web assets.

## 2. Version and tag contract

The following values must agree exactly:

- `[project].version` in `pyproject.toml`;
- `__version__` in `src/s1grits/__version__.py`;
- `version` in `CITATION.cff`;
- the Git tag, with a leading `v`.

Stable tags use `vX.Y.Z`; release candidates use `vX.Y.Z-rcN`, which maps to
the PEP 440 package version `X.Y.ZrcN`. This update targets `4.0.0`;
do not reuse the published `3.0.0` version. A major target names the expanded
product/access/tool interface; it does not require rewriting the production core.

## 3. Prepare the release commit

Work on a feature branch, then:

1. Finish and review `CHANGELOG.md` under `Unreleased`.
2. Close the release-scope decisions above, then set the next version in both
   version files and CITATION.cff; update the citation date for the actual release.
3. Run the offline source gate.
4. Run lint, tests and a local package build.
5. Merge the reviewed commit to `main` and push `main` before tagging.

The local 4.0.0 preparation can use `--candidate-tag v4.0.0` without creating
a Git tag. The following Windows/PowerShell checks precede publication:

```powershell
python tools/release_check.py --candidate-tag v4.0.0
python -m ruff check src tests tools benchmarks
python -m mypy
python -m pytest tests -q --tb=short --basetemp .test-tmp/release-v4 -p no:cacheprovider
python -m build
python -m twine check --strict dist/*
```

Use a fresh dedicated test directory for each run; pytest may clear its basetemp.
Verify wheel/sdist contents exclude manuscripts and research data, then install
the wheel in an isolated environment and run the small-output tutorial and Web
checks there. Source imports must not mask missing packaged resources.

The candidate gate is network-free. It verifies version parity, tag syntax,
required GeoParquet and retained web assets, absence of a
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
git tag -a v4.0.0 -m "S1-GRiTS 4.0.0"
python tools/release_check.py --release-tag v4.0.0
git push origin v4.0.0
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
.\.venv-release-smoke\Scripts\python -m pip install "s1grits[web]==4.0.0"
.\.venv-release-smoke\Scripts\s1grits --version
New-Item -ItemType Directory -Path .test-tmp/release-smoke -Force
.\.venv-release-smoke\Scripts\s1grits init .test-tmp/release-smoke/config.yaml
.\.venv-release-smoke\Scripts\s1grits doctor --config .test-tmp/release-smoke/config.yaml
.\.venv-release-smoke\Scripts\s1grits serve .test-tmp/release-smoke/output --port 5556
```

Confirm that `http://127.0.0.1:5556/` opens the retained Chinese Web and its
existing production/catalog routes still work. Verify new V4 tools through the
CLI/Python tutorial; bilingual switching and new tool forms belong to the later
Web release. Also confirm both artifacts appear on GitHub Releases and PyPI.

If a published build is defective, stop recommending it, yank that exact PyPI
version when appropriate, fix forward with a new patch version, and retain the
old immutable tag for auditability.
