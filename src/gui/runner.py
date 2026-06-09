"""
Subprocess process manager for S1-GRiTS GUI.

Wraps CLI invocations safely:
- Uses list-form Popen (shell=False) to prevent command injection
- Reads stdout/stderr in a background daemon thread via a Queue
- Supports graceful stop (SIGTERM -> SIGKILL after timeout)
- Tracks run state, timing, and return code
"""

import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


# Patterns whose values should be redacted from log output
_SENSITIVE_PATTERN = re.compile(
    r'(password|token|secret|api[_\s]?key|auth[_\s]?key)\s*[:=]\s*\S+',
    re.IGNORECASE,
)


def _sanitize_line(line: str) -> str:
    """Replace sensitive values in a log line with [REDACTED]."""
    return _SENSITIVE_PATTERN.sub(r'\1: [REDACTED]', line)


class CommandRunner:
    """
    Manages a single s1grits CLI subprocess.

    Usage:
        runner = CommandRunner()
        runner.run(["s1grits", "process", "--config", "/tmp/cfg.yaml"])
        while runner.is_running():
            for line in runner.drain_logs():
                print(line)
            time.sleep(0.4)
        print("Exit code:", runner.returncode)
    """

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._log_queue: queue.Queue = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._start_time: float | None = None
        self._end_time: float | None = None
        self.returncode: int | None = None
        self.status: str = "idle"       # idle | running | success | failed | stopped
        self.cmd_args: list[str] = []
        self._stop_requested: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, cmd_args: list[str]) -> None:
        """
        Launch a subprocess with the given argument list.

        Args:
            cmd_args: Full command as a list, e.g. ["s1grits", "process", "--config", "f.yaml"]

        Raises:
            RuntimeError: If a process is already running.
        """
        if self.is_running():
            raise RuntimeError("A process is already running. Stop it first.")

        self._stop_requested = False
        self.cmd_args = cmd_args
        self.returncode = None
        self.status = "running"
        self._start_time = time.time()
        self._end_time = None

        # Clear leftover items from a previous run
        while not self._log_queue.empty():
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                break

        env = os.environ.copy()
        # Force UTF-8 and unbuffered output from Python subprocesses.
        # PYTHONUNBUFFERED=1 disables block-buffering on stdout/stderr,
        # ensuring log lines arrive at the GUI promptly instead of being
        # held in the OS pipe buffer for seconds at a time.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        self._process = subprocess.Popen(
            cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,     # SECURITY: never use shell=True
            env=env,
        )

        self._reader_thread = threading.Thread(
            target=self._read_output,
            daemon=True,
            name="s1grits-log-reader",
        )
        self._reader_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """
        Terminate the running process gracefully, then forcefully.

        Args:
            timeout: Seconds to wait after SIGTERM before sending SIGKILL.
        """
        if self._process is None:
            return
        if self._process.poll() is not None:
            return

        self._stop_requested = True
        try:
            self._process.terminate()          # SIGTERM
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()               # SIGKILL
            self._process.wait()
        finally:
            self.status = "stopped"
            self._end_time = time.time()
            self.returncode = self._process.returncode

    def is_running(self) -> bool:
        """Return True if the subprocess is currently active."""
        return self._process is not None and self._process.poll() is None

    def drain_logs(self) -> list[str]:
        """
        Return all log lines that have arrived since the last call.

        Also updates ``status`` and ``returncode`` when the process exits.

        Returns:
            List of new log lines (may be empty).
        """
        lines: list[str] = []
        try:
            while True:
                lines.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass

        # Check if process has finished
        if not self.is_running() and self.status == "running":
            # Wait for previous reader thread to finish flushing before draining
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=2.0)
            # Drain any remaining lines the reader thread flushed
            try:
                while True:
                    lines.append(self._log_queue.get_nowait())
            except queue.Empty:
                pass
            self._end_time = time.time()
            self.returncode = self._process.returncode if self._process else None
            if self._stop_requested:
                self.status = "stopped"
            else:
                self.status = "success" if self.returncode == 0 else "failed"

        return lines

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    @property
    def started_at(self) -> str:
        """Human-readable start time, or empty string."""
        if self._start_time is None:
            return ""
        return datetime.fromtimestamp(self._start_time).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def elapsed(self) -> str:
        """Human-readable elapsed / total duration, or empty string."""
        if self._start_time is None:
            return ""
        end = self._end_time if self._end_time else time.time()
        secs = int(end - self._start_time)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_output(self) -> None:
        """Background thread: read lines from the subprocess and queue them."""
        if self._process is None or self._process.stdout is None:
            return
        try:
            for raw_line in self._process.stdout:
                line = raw_line.rstrip("\r\n")
                line = _sanitize_line(line)
                self._log_queue.put(line)
        except ValueError:
            # Pipe was closed externally
            pass
        finally:
            # Ensure process state is updated
            if self._process:
                self._process.stdout.close()


def resolve_s1grits_cmd() -> str:
    """
    Return the path to the s1grits executable.

    Prefers the executable in the same virtual-env as the current Python,
    falling back to a plain 'python -m s1grits.cli' invocation.
    """
    # Check for s1grits next to current python
    candidate = Path(sys.executable).parent / (
        "s1grits.exe" if sys.platform == "win32" else "s1grits"
    )
    if candidate.exists():
        return str(candidate)
    return "s1grits"


_VALID_SUBCOMMANDS = frozenset({
    "process",
    "catalog rebuild", "catalog validate", "catalog inspect",
    "tile inspect",
    "mosaic",
})


def build_cmd(subcommand: str, **kwargs) -> list[str]:
    """
    Build a validated s1grits CLI argument list.

    All values come from explicit keyword arguments, never from
    unsanitized user strings joined via shell interpolation.

    Args:
        subcommand: One of 'process', 'catalog rebuild', etc.
        **kwargs: Mapping of CLI flag names (underscores → hyphens) to values.

    Returns:
        list[str] ready for subprocess.Popen.
    """
    if subcommand not in _VALID_SUBCOMMANDS:
        raise ValueError(f"Unknown subcommand: {subcommand!r}")
    cmd = [resolve_s1grits_cmd()]
    # subcommand may contain a space e.g. "catalog rebuild"
    cmd.extend(subcommand.split())

    for key, val in kwargs.items():
        if val is None or val == "":
            continue
        flag = "--" + key.replace("_", "-")
        if isinstance(val, bool):
            if val:
                cmd.append(flag)
        else:
            cmd.extend([flag, str(val)])

    return cmd
