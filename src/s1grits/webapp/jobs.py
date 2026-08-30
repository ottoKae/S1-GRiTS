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
    metadata: dict = field(default_factory=dict)
    commands: list[list[str]] = field(default_factory=list, repr=False)
    command_stages: list[str] = field(default_factory=list, repr=False)
    current_command: int = 0
    completed_commands: list[int] = field(default_factory=list)
    stage: str = "queued"
    attempt_count: int = 1
    interrupted_at: float | None = None
    process_pid: int | None = None
    process_started_at: float | None = None
    events: list[dict] = field(default_factory=list)
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
        result = {
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
            "current_command": self.current_command,
            "command_total": len(self.commands) or 1,
            "completed_commands": list(self.completed_commands),
            "stage": self.stage,
            "attempt_count": self.attempt_count,
            "interrupted_at": self.interrupted_at,
            "recoverable": self.status == "interrupted" or (
                self.status == "failed"
                and self.error == "Server restarted while the job was active"
            ),
        }
        result.update(self.metadata)
        return result


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

    def submit_batch(
        self,
        configs: list[dict],
        *,
        output_dir: Path | str,
        title: str,
        metadata: dict | None = None,
        catalog_gates: bool = True,
    ) -> Job:
        """Queue a server-built v3 workflow followed by Catalog gates.

        This is intentionally separate from :meth:`submit`: callers provide
        parsed configuration mappings, never arbitrary command lines.  Every
        config is confined to ``output_dir``, which itself must remain inside
        the served workspace.
        """
        target = Path(output_dir).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise ValueError("Workflow output directory escapes the served workspace")
        if not configs:
            raise ValueError("At least one process_scenes config is required")

        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True)
        commands: list[list[str]] = []
        stages: list[str] = []
        for index, raw in enumerate(configs, 1):
            cfg = self._sanitize_config_dict(raw, target)
            cfg_path = job_dir / f"config_{index:02d}.yaml"
            cfg_path.write_text(
                yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            commands.append(self._cmd_prefix + ["process_scenes", "--config", str(cfg_path)])
            stages.append("processing")
        if catalog_gates:
            commands.extend([
                self._cmd_prefix + ["catalog", "resync", "--output-dir", str(target)],
                self._cmd_prefix + ["catalog", "validate", "--output-dir", str(target)],
                self._cmd_prefix + ["catalog", "doctor", "--strict", "--output-dir", str(target)],
            ])
            stages.extend(["validating", "validating", "validating"])

        job = Job(
            id=job_id,
            type="workflow",
            title=title,
            job_dir=job_dir,
            metadata=dict(metadata or {}),
            commands=commands,
            command_stages=stages,
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
                job.stage = "cancelled"
                job.ended_at = time.time()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=term_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._persist(job)
        return job.to_dict()

    def recovery_check(self, job_id: str) -> dict:
        """Inspect whether an interrupted job can be resumed safely.

        Recovery never deletes or rebuilds output.  It replays the first
        unfinished whitelisted CLI command; the v3 ``resume``/``skip`` output
        policies make that replay idempotent at the store/month boundary.
        """
        job = self.get(job_id)
        legacy_restart = (
            job.status == "failed"
            and job.error == "Server restarted while the job was active"
        )
        issues: list[str] = []
        warnings: list[str] = []
        if job.status != "interrupted" and not legacy_restart:
            issues.append("只有因服务重启而中断的任务可以恢复")
        if job.process_pid and self._saved_process_alive(job):
            issues.append(f"原 CLI 进程 PID {job.process_pid} 仍在运行，禁止重复写入")

        commands, stages = self._recover_commands(job)
        if not commands:
            issues.append("任务缺少可恢复的受控命令或 YAML 配置")
        config_paths = sorted({
            path
            for command in commands
            for path in (
                Path(part) for part in command
                if str(part).lower().endswith((".yaml", ".yml"))
            )
        })
        missing_configs = [str(path) for path in config_paths if not path.is_file()]
        if missing_configs:
            issues.append("恢复配置不存在：" + "、".join(missing_configs[:3]))
        for config_path in (path for path in config_paths if path.is_file()):
            try:
                config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                output = config.get("output") or {}
                if job.type in ("workflow", "process_scenes") and (
                    output.get("existing_store", "resume") != "resume"
                    or output.get("existing_month", "skip") != "skip"
                ):
                    issues.append("原配置不是 resume/skip 策略，不能保证安全恢复")
                    break
                expected_resolution = job.metadata.get("target_resolution")
                actual_resolution = (config.get("processing") or {}).get("target_resolution")
                if (
                    expected_resolution is not None and actual_resolution is not None
                    and float(expected_resolution) != float(actual_resolution)
                ):
                    issues.append("任务账本与 YAML 的目标分辨率不一致")
                    break
            except (OSError, ValueError, yaml.YAMLError) as exc:
                issues.append(f"恢复配置校验失败：{exc}")
                break

        output_text = str(job.metadata.get("output_dir") or self.root)
        output_dir = Path(output_text).resolve()
        if output_dir != self.root and not output_dir.is_relative_to(self.root):
            issues.append("原任务输出目录已越过当前服务工作区")
        active_conflicts = [
            other.id for other in self._jobs.values()
            if other.id != job.id
            and other.status in ("queued", "running", "detached")
            and Path(str(other.metadata.get("output_dir") or self.root)).resolve() == output_dir
        ]
        if active_conflicts:
            issues.append("同一输出目录已有活动任务：" + "、".join(active_conflicts[:3]))

        zarr_count = 0
        catalog_exists = False
        if output_dir.is_dir():
            zarr_count = sum(1 for _ in output_dir.rglob("*.zarr"))
            catalog_exists = (output_dir / "catalog.parquet").is_file()
            if zarr_count:
                warnings.append(f"检测到 {zarr_count} 个现有 Zarr，将按 resume/skip 续做")
        else:
            warnings.append("输出目录尚不存在，恢复时将重新创建")

        start_index = self._resume_start(job, stages)
        return {
            "job_id": job.id,
            "recoverable": not issues,
            "status": job.status,
            "stage": job.stage,
            "attempt_next": job.attempt_count + 1,
            "resume_command": start_index + 1 if commands else 0,
            "command_total": len(commands),
            "output_dir": str(output_dir),
            "zarr_count": zarr_count,
            "catalog_exists": catalog_exists,
            "issues": issues,
            "warnings": warnings,
            "message": (
                "检查通过；完整成果将保留，中断附近内容可能重新处理"
                if not issues else "当前任务不能安全恢复"
            ),
        }

    def resume(self, job_id: str) -> Job:
        """Manually requeue an interrupted job after a safety inspection."""
        # Serialize the check-and-transition so two browser clicks cannot
        # enqueue the same job twice, and two interrupted jobs targeting the
        # same output cannot both pass the conflict check.
        with self._lock:
            job = self.get(job_id)
            check = self.recovery_check(job_id)
            if not check["recoverable"]:
                raise ValueError("；".join(check["issues"]))
            commands, stages = self._recover_commands(job)
            start_index = self._resume_start(job, stages)
            with job._lock:
                job.commands = commands
                job.command_stages = stages
                job.current_command = start_index
                job.status = "queued"
                job.stage = "queued"
                job.error = None
                job.returncode = None
                job.ended_at = None
                job.attempt_count += 1
                job.process_pid = None
                job.process_started_at = None
                job.metadata["resume_from_command"] = start_index + 1
                job.events.append({
                    "timestamp": time.time(),
                    "event": "task_resumed",
                    "level": "info",
                    "stage": "queued",
                    "message": f"人工恢复，第 {job.attempt_count} 次执行",
                })
        self._persist(job)
        self._queue.put(job.id)
        return job

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
        return self._sanitize_config_dict(cfg, self.root)

    def _sanitize_config_dict(self, cfg: dict, output_dir: Path) -> dict:
        """Copy and confine one parsed config to an approved output root."""
        cfg = json.loads(json.dumps(cfg))
        out = cfg.setdefault("output", {})
        if not isinstance(out, dict):
            raise ValueError("output section must be a mapping")
        requested = out.get("base_dir")
        out["base_dir"] = str(output_dir)
        if requested and Path(str(requested)).resolve() != output_dir:
            logger.info(
                "Job config output.base_dir %r overridden to workspace %s",
                requested, output_dir,
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
        log_path = job.job_dir / "log.txt"
        resume_index = max(0, int(job.metadata.pop("resume_from_command", 1)) - 1)
        job.status = "running"
        job.stage = "processing"
        job.started_at = time.time()
        job.events.append({
            "timestamp": job.started_at,
            "event": "task_attempt_started",
            "level": "info",
            "stage": "processing",
            "message": f"第 {job.attempt_count} 次执行开始",
        })
        self._persist(job)

        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        try:
            commands = job.commands or [self._build_args(job)]
            stages = job.command_stages or ["processing"]
            log_mode = "a" if log_path.exists() and job.attempt_count > 1 else "w"
            with open(log_path, log_mode, encoding="utf-8") as log_fp:
                if log_mode == "a":
                    log_fp.write(
                        f"\n[WEB] attempt {job.attempt_count} resumed after interruption "
                        f"from command {resume_index + 1}/{len(commands)}\n"
                    )
                    log_fp.flush()
                for index, args in enumerate(commands[resume_index:], resume_index + 1):
                    if job._cancel:
                        break
                    job.current_command = index
                    job.stage = stages[index - 1]
                    log_fp.write(
                        f"[WEB] command {index}/{len(commands)} stage={job.stage}\n"
                    )
                    log_fp.flush()
                    self._persist(job)
                    proc = subprocess.Popen(
                        args,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        bufsize=1, shell=False, env=env, cwd=str(self.root),
                    )
                    with job._lock:
                        job._proc = proc
                        job.process_pid = proc.pid
                        try:
                            import psutil

                            job.process_started_at = psutil.Process(proc.pid).create_time()
                        except Exception:
                            job.process_started_at = time.time()
                    self._persist(job)
                    assert proc.stdout is not None
                    for raw in proc.stdout:
                        line = _SENSITIVE.sub(r"\1: [REDACTED]", raw.rstrip("\r\n"))
                        log_fp.write(line + "\n")
                        log_fp.flush()
                        job._log_lines += 1
                        self._update_progress(job, line)
                    proc.wait()
                    job.returncode = proc.returncode
                    if proc.returncode != 0:
                        break
                    if index not in job.completed_commands:
                        job.completed_commands.append(index)
                    self._persist(job)
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
                job.stage = "cancelled"
            elif job.returncode == 0:
                job.status = "success"
                job.stage = "done"
            else:
                job.status = "failed"
                job.stage = "failed"
            with job._lock:
                job._proc = None
                job.process_pid = None
                job.process_started_at = None
                job.events.append({
                    "timestamp": job.ended_at,
                    "event": f"task_attempt_{job.status}",
                    "level": "info" if job.status == "success" else "error",
                    "stage": job.stage,
                    "message": (
                        f"第 {job.attempt_count} 次执行完成"
                        if job.status == "success"
                        else job.error or f"第 {job.attempt_count} 次执行结束：{job.status}"
                    ),
                })
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
            payload = job.to_dict()
            payload["commands"] = job.commands
            payload["command_stages"] = job.command_stages
            payload["process_pid"] = job.process_pid
            payload["process_started_at"] = job.process_started_at
            payload["events"] = job.events
            target = job.job_dir / "job.json"
            temporary = job.job_dir / "job.json.tmp"
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, target)
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
            job.metadata = data.get("metadata") or {
                key: data[key]
                for key in (
                    "workflow", "directions", "tiles", "years", "months",
                    "output_subdir", "output_dir", "include_static",
                    "static_layers", "plan_summary",
                )
                if key in data
            }
            job.current_command = int(data.get("current_command") or 0)
            job.completed_commands = [int(v) for v in data.get("completed_commands", [])]
            job.stage = data.get("stage") or "failed"
            job.attempt_count = max(1, int(data.get("attempt_count") or 1))
            job.interrupted_at = data.get("interrupted_at")
            job.process_pid = data.get("process_pid")
            job.process_started_at = data.get("process_started_at")
            job.events = list(data.get("events") or [])
            job.commands = [list(command) for command in data.get("commands") or []]
            job.command_stages = list(data.get("command_stages") or [])
            status = data.get("status", "failed")
            if status == "running":
                if self._saved_process_alive(job):
                    job.status = "detached"
                    job.error = "服务已重启，但原 CLI 进程仍在运行；暂不允许重复执行"
                else:
                    job.status = "interrupted"
                    job.interrupted_at = job.interrupted_at or time.time()
                    job.error = "服务重启导致任务中断；已有成果可保留并安全恢复"
                    job.events.append({
                        "timestamp": job.interrupted_at,
                        "event": "task_interrupted",
                        "level": "error",
                        "stage": job.stage,
                        "message": job.error,
                    })
            elif status == "queued":
                job.status = "queued"
                job.error = None
            else:
                job.status = status
            with self._lock:
                self._jobs[job.id] = job
                self._order.append(job.id)
            self._persist(job)
            if job.status == "queued":
                self._queue.put(job.id)

    @staticmethod
    def _saved_process_alive(job: Job) -> bool:
        if not job.process_pid:
            return False
        try:
            import psutil

            process = psutil.Process(int(job.process_pid))
            if not process.is_running():
                return False
            if job.process_started_at is None:
                return True
            return abs(process.create_time() - float(job.process_started_at)) < 2.0
        except Exception:
            return False

    def _recover_commands(self, job: Job) -> tuple[list[list[str]], list[str]]:
        commands = [list(command) for command in job.commands]
        stages = list(job.command_stages)
        if commands:
            return commands, stages or ["processing"] * len(commands)
        if job.type == "workflow" and job.job_dir:
            configs = sorted(job.job_dir.glob("config_*.yaml"))
            commands = [
                self._cmd_prefix + ["process_scenes", "--config", str(path)]
                for path in configs
            ]
            stages = ["processing"] * len(commands)
            output = str(job.metadata.get("output_dir") or self.root)
            commands.extend([
                self._cmd_prefix + ["catalog", "resync", "--output-dir", output],
                self._cmd_prefix + ["catalog", "validate", "--output-dir", output],
                self._cmd_prefix + ["catalog", "doctor", "--strict", "--output-dir", output],
            ])
            stages.extend(["validating", "validating", "validating"])
            return commands, stages
        if job.type in JOB_TYPES and job.job_dir:
            try:
                return [self._build_args(job)], ["processing"]
            except Exception:
                pass
        return [], []

    @staticmethod
    def _resume_start(job: Job, stages: list[str]) -> int:
        if not stages:
            return 0
        current = max(0, min(len(stages) - 1, int(job.current_command or 1) - 1))
        if job.stage in ("validating", "associating"):
            return next((i for i, stage in enumerate(stages) if stage == "validating"), current)
        return current

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
