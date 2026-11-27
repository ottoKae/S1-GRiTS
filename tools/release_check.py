"""Offline release-readiness gate for S1-GRiTS maintainers.

Run the source/package contract on any branch::

    python tools/release_check.py

Before creating a release tag, first bump both version files and run::

    python tools/release_check.py --candidate-tag v3.1.0

After committing on ``main`` and creating the annotated tag, run the strict
gate before pushing it::

    python tools/release_check.py --release-tag v3.1.0

The script is deliberately network-free. GitHub Actions remains responsible
for testing three operating systems and publishing with PyPI Trusted
Publishing; this gate catches local version, frontend and repository-state
mistakes before that workflow starts.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
CITATION_VERSION_PATTERN = re.compile(
    r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$', re.MULTILINE
)
TAG_PATTERN = re.compile(r"^v(?P<version>\d+\.\d+\.\d+(?:-rc\d+)?)$")
EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}(?![\w.-])"
)
USER_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:(?:\\\\|\\)Users(?:\\\\|\\)[^\\/\s\"']+"),
    re.compile(r"(?i)(?<![A-Za-z0-9_])/(?:home|Users)/[A-Za-z0-9._-]+"),
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,255}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
)
TEXT_SCAN_LIMIT = 8 * 1024 * 1024
SKIP_PRIVACY_SUFFIXES = {
    ".7z", ".bz2", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".npz",
    ".parquet", ".pdf", ".png", ".tar", ".tif", ".tiff", ".whl", ".xz", ".zip",
}


@dataclass(frozen=True)
class Result:
    level: str
    name: str
    detail: str


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _versions() -> tuple[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project_version = tomllib.load(stream)["project"]["version"]
    module_text = (ROOT / "src/s1grits/__version__.py").read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(module_text)
    if not match:
        raise ValueError("src/s1grits/__version__.py has no literal __version__")
    return str(project_version), match.group(1)


def _citation_version() -> str:
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    match = CITATION_VERSION_PATTERN.search(citation)
    if not match:
        raise ValueError("CITATION.cff has no top-level version")
    return match.group(1)


def _pep440_from_tag(tag: str) -> str | None:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        return None
    return match.group("version").replace("-rc", "rc")


def _tracked_text_files() -> list[tuple[str, str]]:
    """Return small tracked text files without echoing sensitive contents."""
    completed = _git("ls-files", "-z")
    if completed.returncode != 0:
        return []
    files: list[tuple[str, str]] = []
    for relative in completed.stdout.split("\0"):
        if not relative:
            continue
        path = ROOT / relative
        if (
            not path.is_file()
            or path.suffix.casefold() in SKIP_PRIVACY_SUFFIXES
            or path.stat().st_size > TEXT_SCAN_LIMIT
        ):
            continue
        payload = path.read_bytes()
        if b"\0" in payload:
            continue
        files.append((relative.replace("\\", "/"), payload.decode("utf-8", errors="replace")))
    return files


def _privacy_contract() -> list[Result]:
    """Reject high-confidence secrets, contact emails and user-home paths."""
    email_files: set[str] = set()
    user_path_files: set[str] = set()
    secret_files: set[str] = set()
    for relative, content in _tracked_text_files():
        if EMAIL_PATTERN.search(content):
            email_files.add(relative)
        if any(pattern.search(content) for pattern in USER_PATH_PATTERNS):
            user_path_files.add(relative)
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            secret_files.add(relative)

    def result(name: str, matches: set[str], clean_detail: str) -> Result:
        return Result(
            "FAIL" if matches else "PASS",
            name,
            "tracked file(s): " + ", ".join(sorted(matches)) if matches else clean_detail,
        )

    results = [
        result("privacy: email addresses", email_files, "none in tracked source"),
        result("privacy: user-home paths", user_path_files, "none in tracked source"),
        result("privacy: high-confidence secrets", secret_files, "none in tracked source"),
    ]

    configured = _git("config", "--get", "user.email")
    email = configured.stdout.strip()
    if not email:
        detail = "no repository Git email configured"
        level = "PASS"
    elif email.casefold().endswith("@users.noreply.github.com"):
        detail = "repository Git email uses GitHub no-reply"
        level = "PASS"
    else:
        detail = "repository Git email is not a GitHub no-reply address"
        level = "WARN"
    results.append(Result(level, "privacy: future commit identity", detail))
    return results


def _source_contract() -> list[Result]:
    results: list[Result] = []
    project_version, module_version = _versions()
    level = "PASS" if project_version == module_version else "FAIL"
    results.append(Result(level, "version parity", f"pyproject={project_version}, module={module_version}"))
    citation_version = _citation_version()
    results.append(Result(
        "PASS" if citation_version == project_version else "FAIL",
        "citation version parity",
        f"CITATION.cff={citation_version}, package={project_version}",
    ))

    current_tag = f"v{project_version}"
    tag_ref = _git("rev-parse", "--verify", f"refs/tags/{current_tag}")
    if tag_ref.returncode == 0:
        results.append(Result(
            "WARN",
            "current version tag",
            f"{current_tag} already exists; bump both version files before the next release",
        ))

    required = [
        "src/s1grits/data/mgrs.parquet",
        "src/s1grits/data/jpl_burst_geo.parquet",
        "src/s1grits/data/mgrs_burst_lookup_table.parquet",
        "src/s1grits/webapp/static/index.html",
        "src/s1grits/webapp/static/app.js",
        "src/s1grits/webapp/static/styles.css",
        "src/s1grits/webapp/static/leaflet/leaflet.js",
        "src/s1grits/webapp/static/leaflet/leaflet.css",
        "src/s1grits/webapp/static/logo-mark.png",
    ]
    missing = [path for path in required if not (ROOT / path).is_file()]
    results.append(Result(
        "FAIL" if missing else "PASS",
        "package assets",
        "missing: " + ", ".join(missing) if missing else f"{len(required)} required files present",
    ))

    index = (ROOT / "src/s1grits/webapp/static/index.html").read_text(encoding="utf-8")
    chinese = 'lang="zh-CN"' in index
    results.append(Result(
        "PASS" if chinese else "FAIL",
        "canonical frontend",
        "bundled SPA declares zh-CN" if chinese else "index.html must declare lang=zh-CN",
    ))

    tracked = _git("ls-files", "src").stdout.replace("\\", "/").splitlines()
    legacy = [
        path for path in tracked
        if path.startswith("src/gui/")
        or "/webapp_en/" in path.casefold()
        or "/gui_en/" in path.casefold()
    ]
    results.append(Result(
        "FAIL" if legacy else "PASS",
        "legacy frontend",
        "tracked: " + ", ".join(legacy) if legacy else "no legacy English/Streamlit frontend tracked",
    ))

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_rules = all(token in pyproject for token in (
        '"s1grits.webapp"',
        '"static/*.html"',
        '"static/leaflet/*.js"',
    ))
    results.append(Result(
        "PASS" if package_rules else "FAIL",
        "package-data rules",
        "web static package-data rules present" if package_rules else "web package-data rules incomplete",
    ))

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    onboarding_tokens = ('s1grits[web]', "s1grits init", "s1grits serve")
    missing_docs = [
        token for token in onboarding_tokens
        if token not in readme or token not in readme_zh
    ]
    results.append(Result(
        "FAIL" if missing_docs else "PASS",
        "wheel-first quick start",
        "missing in both READMEs: " + ", ".join(missing_docs)
        if missing_docs else "install, init and Chinese GUI commands documented",
    ))
    results.extend(_privacy_contract())
    return results


def _tag_contract(tag: str, strict: bool) -> list[Result]:
    results: list[Result] = []
    project_version, _ = _versions()
    mapped = _pep440_from_tag(tag)
    results.append(Result(
        "PASS" if mapped else "FAIL",
        "tag syntax",
        f"{tag} -> {mapped}" if mapped else "expected vX.Y.Z or vX.Y.Z-rcN",
    ))
    if mapped:
        results.append(Result(
            "PASS" if mapped == project_version else "FAIL",
            "tag/version parity",
            f"tag={mapped}, package={project_version}",
        ))

    if not strict:
        existing = _git("rev-parse", "--verify", f"refs/tags/{tag}")
        results.append(Result(
            "FAIL" if existing.returncode == 0 else "PASS",
            "unused candidate tag",
            f"{tag} already exists and must never be moved or reused"
            if existing.returncode == 0 else f"{tag} is not present locally",
        ))
        return results

    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    clean = status.returncode == 0 and not status.stdout.strip()
    results.append(Result(
        "PASS" if clean else "FAIL",
        "clean worktree",
        "clean" if clean else "commit or remove every tracked/untracked release input first",
    ))

    tag_ref = _git("rev-parse", "--verify", f"refs/tags/{tag}")
    tag_exists = tag_ref.returncode == 0
    results.append(Result(
        "PASS" if tag_exists else "FAIL",
        "local release tag",
        tag_ref.stdout.strip() if tag_exists else f"annotated tag {tag} does not exist",
    ))
    if tag_exists:
        tag_commit = _git("rev-list", "-n", "1", tag).stdout.strip()
        head = _git("rev-parse", "HEAD").stdout.strip()
        results.append(Result(
            "PASS" if tag_commit == head else "FAIL",
            "tag points to HEAD",
            f"tag={tag_commit[:12]}, HEAD={head[:12]}",
        ))
        tag_type = _git("cat-file", "-t", f"refs/tags/{tag}").stdout.strip()
        results.append(Result(
            "PASS" if tag_type == "tag" else "FAIL",
            "annotated tag",
            f"object type={tag_type or 'unknown'}",
        ))

    ancestor = _git("merge-base", "--is-ancestor", "HEAD", "origin/main")
    results.append(Result(
        "PASS" if ancestor.returncode == 0 else "FAIL",
        "main ancestry",
        "HEAD is reachable from origin/main"
        if ancestor.returncode == 0 else "merge and push the release commit to main first",
    ))
    author_email = _git("show", "-s", "--format=%ae", "HEAD").stdout.strip()
    private_author = bool(
        author_email and author_email.casefold().endswith("@users.noreply.github.com")
    )
    results.append(Result(
        "PASS" if private_author else "FAIL",
        "release commit privacy",
        "HEAD author email uses GitHub no-reply"
        if private_author else "configure GitHub no-reply before creating the release commit",
    ))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--candidate-tag",
        help="Check tag syntax/version before the tag exists (for example v3.1.0)",
    )
    group.add_argument(
        "--release-tag",
        help="Strictly require a clean, annotated tag on an origin/main commit",
    )
    args = parser.parse_args(argv)

    results = _source_contract()
    if args.candidate_tag:
        results.extend(_tag_contract(args.candidate_tag, strict=False))
    if args.release_tag:
        results.extend(_tag_contract(args.release_tag, strict=True))

    for result in results:
        print(f"[{result.level}] {result.name}: {result.detail}")
    failures = sum(result.level == "FAIL" for result in results)
    warnings = sum(result.level == "WARN" for result in results)
    print(
        f"\nRelease gate: {'FAIL' if failures else 'PASS'} "
        f"({failures} failure(s), {warnings} warning(s))"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
