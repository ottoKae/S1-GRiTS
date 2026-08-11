"""Server-side job manager: whitelisted CLI invocations with a bounded queue.

Design (see docs/webapp_v2_architecture.md §2.2):

- A job IS a CLI run. The manager never reimplements pipeline logic; it
  supervises ``s1grits <subcommand>`` exactly as a user would run it, so the
  web UI inherits the CLI's retry/resume/locking behaviour unchanged.
- Whitelist only: job types map to fixed argument vectors. User input is
  confined to a YAML config that is safe-loaded, sanity-checked, and written
  by the server with ``output.base_dir`` FORCED to the workspace root.
- Bounded FIFO queue with configurable concurrency (default 1 — the pipeline
  parallelises internally via ``parallel.max_workers``; stacking whole runs
  multiplies memory).
- Jobs are server-owned: they survive browser refreshes, logs stream to
  per-job files under ``{root}/.webapp/jobs/{id}/`` and are served
  incrementally, and a ledger (``job.json``) records the outcome for
  history across server restarts.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Values of credential-looking keys are redacted from log lines (same policy
# as the v1.0 GUI runner).
_SENSITIVE = re.compile(
    r"(password|token|secret|api[_\s]?key|auth[_\s]?key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)

# Progress markers the pipeline already emits for its own diagnostics.
_BATCH_RE = re.compile(r"--- Batch (\d+)/(\d+) ---")
_PHASE_RE = re.compile(r"tile=([0-9A-Za-z_]+) batch=(\d+)/(\d+)")

# Job types -> CLI argument template. {config} is replaced by the path of the
# server-written config file; {root} by the workspace root.
JOB_TYPES: dict[str, dict] = {
    "process_scenes": {
        "args": ["process_scenes", "--config", "{config}"],
        "needs_config": True,
        "title": "Scenes / monthly processing",
    },
    "process": {
        "args": ["process", "--config", "{config}"],
        "needs_config": True,
        "title": "Monthly workflow",
    },
    "catalog_resync": {
        # NOTE: the CLI flag is --output-dir (a --dir job fails at argparse
        # before doing anything).
        "args": ["catalog", "resync", "--output-dir", "{root}"],
        "needs_config": False,
        "title": "Catalog resync",
    },
    "doctor": {
        "args": ["doctor", "--config", "{config}"],
        "needs_config": True,
        "title": "Preflight doctor",
    },
}


def _s1grits_cmd() -> list[str]:
    """Command prefix for the CLI, preferring the current environment."""
    exe = Path(sys.executable).parent / (
        "s1grits.exe" if sys.platform == "win32" else "s1grits"
    )
    if exe.exists():
        return [str(exe)]
    return [sys.executable, "-m", "s1grits.cli"]


@dataclass
class Job:
    id: str
    type: str
    title: str
    status: str = "queued"        # queued | running | success | failed | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    returncode: int | None = None
    error: str | None = None
    # {tile: [batch, total]} plus derived pct
    progress: dict = field(default_factory=dict)
    job_dir: Path | None = None
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _cancel: bool = field(default=False, repr=False)
    _log_lines: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict:
        with self._lock:
            per_tile = {t: list(v) for t, v in self.progress.items()}
        done = sum(v[0] for v in per_tile.values())
        total = sum(v[1] for v in per_tile.values())
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "returncode": self.returncode,
            "error": self.error,
            "log_lines": self._log_lines,
            "progress": {
                "per_tile": per_tile,
                "pct": round(100.0 * done / total, 1) if total else None,
            },
        }


class JobManager:
    """Bounded FIFO of supervised CLI jobs for one workspace."""

    def __init__(self, root: Path | str, max_concurrent: int = 1,
                 cmd_prefix: list[str] | None = None):
        self.root = Path(root).resolve()
        self.jobs_dir = self.root / ".webapp" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._cmd_prefix = cmd_prefix or _s1grits_cmd()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._workers = [
            threading.Thread(target=self._worker, daemon=True,
                             name=f"s1grits-job-worker-{i}")
            for i in range(max(1, int(max_concurrent)))
        ]
        for w in self._workers:
            w.start()
        self._load_history()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, job_type: str, config_text: str | None = None,
               title: str | None = None) -> Job:
        spec = JOB_TYPES.get(job_type)
        if spec is None:
            raise ValueError(
                f"Unknown job type {job_type!r}; allowed: {sorted(JOB_TYPES)}"
            )
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True)

        if spec["needs_config"]:
            cfg = self._sanitize_config(config_text)
            (job_dir / "config.yaml").write_text(
                yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
            )

        job = Job(
            id=job_id, type=job_type,
            title=title or spec["title"], job_dir=job_dir,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
        self._persist(job)
        self._queue.put(job_id)
        return job

    def list(self) -> list[dict]:
        with self._lock:
            ids = list(self._order)
        return [self._jobs[i].to_dict() for i in reversed(ids)]

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def log(self, job_id: str, after: int = 0, limit: int = 500) -> dict:
        """Incremental log read: lines with index >= ``after``."""
        job = self.get(job_id)
        log_path = job.job_dir / "log.txt"
        lines: list[str] = []
        next_after = after
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8", errors="replace") as fp:
                for i, line in enumerate(fp):
                    if i >= after and len(lines) < limit:
                        lines.append(line.rstrip("\n"))
                    next_after = i + 1
        return {"after": after, "next": max(next_after, after + len(lines)),
                "lines": lines, "status": job.status}

    def cancel(self, job_id: str, term_timeout: float = 8.0) -> dict:
        job = self.get(job_id)
        with job._lock:
            job._cancel = True
            proc = job._proc
            if job.status == "queued":
                job.status = "cancelled"
                job.ended_at = time.time()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=term_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._persist(job)
        return job.to_dict()

    # ------------------------------------------------------------------
    # Config sanitisation
    # ------------------------------------------------------------------

    def _sanitize_config(self, config_text: str | None) -> dict:
        """Parse and confine a user-submitted YAML config.

        ``output.base_dir`` is FORCED to the workspace root so no job can
        write outside the workspace the server was pointed at.
        """
        if not config_text or not config_text.strip():
            raise ValueError("This job type requires a YAML config")
        try:
            cfg = yaml.safe_load(config_text)
        except yaml.YAMLError as exc:
            raise ValueError(f"Config is not valid YAML: {exc}") from exc
        if not isinstance(cfg, dict):
            raise ValueError("Config must be a YAML mapping")
        out = cfg.setdefault("output", {})
        if not isinstance(out, dict):
            raise ValueError("output section must be a mapping")
        requested = out.get("base_dir")
        out["base_dir"] = str(self.root)
        if requested and Path(str(requested)).resolve() != self.root:
            logger.info(
                "Job config output.base_dir %r overridden to workspace %s",
                requested, self.root,
            )
        return cfg

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                continue
            if job._cancel or job.status == "cancelled":
                continue
            self._run(job)

    def _build_args(self, job: Job) -> list[str]:
        spec = JOB_TYPES[job.type]
        cfg_path = str(job.job_dir / "config.yaml")
        args = [
            a.replace("{config}", cfg_path).replace("{root}", str(self.root))
            for a in spec["args"]
        ]
        return self._cmd_prefix + args

    def _run(self, job: Job) -> None:
        args = self._build_args(job)
        log_path = job.job_dir / "log.txt"
        job.status = "running"
        job.started_at = time.time()
        self._persist(job)

        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        try:
            with open(log_path, "w", encoding="utf-8") as log_fp:
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    bufsize=1, shell=False, env=env, cwd=str(self.root),
                )
                with job._lock:
                    job._proc = proc
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = _SENSITIVE.sub(r"\1: [REDACTED]", raw.rstrip("\r\n"))
                    log_fp.write(line + "\n")
                    log_fp.flush()
                    job._log_lines += 1
                    self._update_progress(job, line)
                proc.wait()
                job.returncode = proc.returncode
        except FileNotFoundError as exc:
            job.error = f"CLI executable not found: {exc}"
            job.returncode = -1
        except Exception as exc:  # pragma: no cover - defensive
            job.error = str(exc)
            job.returncode = -1
        finally:
            job.ended_at = time.time()
            if job._cancel:
                job.status = "cancelled"
            elif job.returncode == 0:
                job.status = "success"
            else:
                job.status = "failed"
            with job._lock:
                job._proc = None
            self._persist(job)

    # Track state per (current tile context) so bare "--- Batch i/n ---"
    # lines (which do not carry the tile) attribute to the last tile seen.
    def _update_progress(self, job: Job, line: str) -> None:
        m = _PHASE_RE.search(line)
        if m:
            tile, cur, total = m.group(1), int(m.group(2)), int(m.group(3))
            with job._lock:
                prev = job.progress.get(tile, [0, total])
                job.progress[tile] = [max(prev[0], cur), total]
            return
        m = _BATCH_RE.search(line)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            with job._lock:
                # No tile context on this marker: keep a synthetic lane so
                # single-tile runs still show progress before PHASE lines.
                prev = job.progress.get("_run", [0, total])
                job.progress["_run"] = [max(prev[0], cur), total]

    # ------------------------------------------------------------------
    # Persistence (history across restarts)
    # ------------------------------------------------------------------

    def _persist(self, job: Job) -> None:
        try:
            (job.job_dir / "job.json").write_text(
                json.dumps(job.to_dict(), indent=2), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not persist job %s: %s", job.id, exc)

    def _load_history(self) -> None:
        for d in sorted(self.jobs_dir.iterdir()) if self.jobs_dir.exists() else []:
            meta = d / "job.json"
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            job = Job(
                id=data.get("id", d.name), type=data.get("type", "?"),
                title=data.get("title", d.name), job_dir=d,
            )
            job.created_at = data.get("created_at") or 0.0
            job.started_at = data.get("started_at")
            job.ended_at = data.get("ended_at")
            job.returncode = data.get("returncode")
            job.error = data.get("error")
            job._log_lines = int(data.get("log_lines") or 0)
            job.progress = {
                t: list(v)
                for t, v in (data.get("progress", {}).get("per_tile", {})).items()
            }
            status = data.get("status", "failed")
            # A job that was 'running' when the server died is orphaned.
            job.status = "failed" if status in ("running", "queued") else status
            if job.status == "failed" and status in ("running", "queued"):
                job.error = job.error or "Server restarted while the job was active"
            with self._lock:
                self._jobs[job.id] = job
                self._order.append(job.id)

    def prune(self, keep: int = 50) -> int:
        """Delete finished job dirs beyond the newest ``keep`` (history cap)."""
        with self._lock:
            finished = [
                self._jobs[i] for i in self._order
                if self._jobs[i].status in ("success", "failed", "cancelled")
            ]
        removed = 0
        for job in finished[:-keep] if keep else finished:
            try:
                shutil.rmtree(job.job_dir, ignore_errors=True)
                with self._lock:
                    self._jobs.pop(job.id, None)
                    if job.id in self._order:
                        self._order.remove(job.id)
                removed += 1
            except OSError:  # pragma: no cover
                pass
        return removed
