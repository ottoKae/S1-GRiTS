"""Parse S1-GRiTS ``[PHASE]`` log lines into a per-tile / per-phase table.

The scenes workflow instruments expensive phases with ``_phase_timer`` in
``workflow_scenes.py``, emitting lines like::

    2026-07-05 00:25:02 - s1_processor.s1grits.workflow_scenes - INFO - \
        [PHASE] metadata.query START tile=17MPU rss_mb=156.8
    2026-07-05 00:25:16 - s1_processor.s1grits.workflow_scenes - INFO - \
        [PHASE] metadata.query END elapsed_s=14.70 rss_mb=321.3 \
        delta_mb=164.5 tile=17MPU rows=144

This module turns those lines into structured records so optimization work can
be evaluated against **real runtime evidence** (elapsed seconds and RSS per
phase per tile) instead of assumptions.  It is a read-only diagnostic tool: it
never imports the workflow and has no side effects on any run.

Usage::

    python -m benchmarks.phase_timing logs/s1grits_scenes_*.log
    python -m benchmarks.phase_timing --json logs/run.log > phases.json

The parser is deterministic and unit-tested (``tests/test_phase_timing.py``);
the CLI summary is a diagnostic.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

# Matches the message body after the logging prefix, e.g.
#   "[PHASE] write.smonthly END elapsed_s=2440.77 rss_mb=6858.1 ..."
_PHASE_RE = re.compile(r"\[PHASE\]\s+(?P<name>\S+)\s+(?P<event>START|END|FAILED)\b(?P<rest>.*)$")
_KV_RE = re.compile(r"(\w+)=([^\s]+)")

# Fields that are numeric when present; everything else stays a string.
_NUMERIC_FIELDS = {"elapsed_s", "rss_mb", "delta_mb", "rows", "scenes", "profiles"}


@dataclass
class PhaseRecord:
    """One parsed ``[PHASE]`` event line."""

    name: str
    event: str  # START | END | FAILED
    fields: dict = field(default_factory=dict)

    @property
    def tile(self) -> str | None:
        return self.fields.get("tile")

    @property
    def elapsed_s(self) -> float | None:
        v = self.fields.get("elapsed_s")
        return float(v) if isinstance(v, (int, float)) else None

    @property
    def rss_mb(self) -> float | None:
        v = self.fields.get("rss_mb")
        return float(v) if isinstance(v, (int, float)) else None


def _coerce(key: str, value: str):
    """Turn a ``key=value`` token value into a float when it is numeric.

    ``rss_mb=na`` (psutil unavailable) and other non-numeric values stay as
    strings so downstream code can distinguish "missing" from a real number.
    """
    if key in _NUMERIC_FIELDS:
        try:
            return float(value)
        except ValueError:
            return value
    return value


def parse_phase_line(line: str) -> PhaseRecord | None:
    """Parse a single log line into a :class:`PhaseRecord`, or ``None``.

    Any line without a well-formed ``[PHASE] <name> START|END|FAILED`` marker
    (blank lines, unrelated log output, truncated lines) returns ``None``.
    """
    m = _PHASE_RE.search(line)
    if not m:
        return None
    fields: dict = {}
    for k, v in _KV_RE.findall(m.group("rest")):
        fields[k] = _coerce(k, v)
    return PhaseRecord(name=m.group("name"), event=m.group("event"), fields=fields)


def parse_phase_lines(lines: Iterable[str]) -> list[PhaseRecord]:
    """Parse an iterable of log lines, skipping non-phase lines."""
    out: list[PhaseRecord] = []
    for line in lines:
        rec = parse_phase_line(line)
        if rec is not None:
            out.append(rec)
    return out


@dataclass
class PhaseSummary:
    """Aggregated timing/memory for a (tile, phase) pair."""

    tile: str
    phase: str
    count: int = 0
    elapsed_s: float = 0.0
    peak_rss_mb: float | None = None
    max_delta_mb: float | None = None
    failed: int = 0


def summarize(records: Iterable[PhaseRecord]) -> list[PhaseSummary]:
    """Aggregate END/FAILED records by (tile, phase).

    START records are ignored for timing (they carry no ``elapsed_s``); only
    END/FAILED lines contribute.  ``peak_rss_mb`` is the max RSS observed at any
    END of that (tile, phase); ``elapsed_s`` is the sum (batches accumulate).
    """
    acc: dict[tuple[str, str], PhaseSummary] = {}
    for r in records:
        if r.event == "START":
            continue
        tile = r.tile or "?"
        key = (tile, r.name)
        s = acc.setdefault(key, PhaseSummary(tile=tile, phase=r.name))
        s.count += 1
        if r.event == "FAILED":
            s.failed += 1
        if r.elapsed_s is not None:
            s.elapsed_s += r.elapsed_s
        if r.rss_mb is not None:
            s.peak_rss_mb = r.rss_mb if s.peak_rss_mb is None else max(s.peak_rss_mb, r.rss_mb)
        dv = r.fields.get("delta_mb")
        if isinstance(dv, (int, float)):
            s.max_delta_mb = dv if s.max_delta_mb is None else max(s.max_delta_mb, dv)
    return sorted(acc.values(), key=lambda s: (s.tile, -s.elapsed_s))


def format_table(summaries: list[PhaseSummary]) -> str:
    """Render summaries as a fixed-width text table with per-tile totals."""
    if not summaries:
        return "(no [PHASE] records found)"
    rows = [("TILE", "PHASE", "N", "ELAPSED_S", "PEAK_RSS_MB", "MAX_DELTA_MB", "FAIL")]
    tiles: dict[str, float] = {}
    for s in summaries:
        tiles[s.tile] = tiles.get(s.tile, 0.0) + s.elapsed_s
        rows.append((
            s.tile, s.phase, str(s.count), f"{s.elapsed_s:.2f}",
            "na" if s.peak_rss_mb is None else f"{s.peak_rss_mb:.1f}",
            "na" if s.max_delta_mb is None else f"{s.max_delta_mb:.1f}",
            str(s.failed),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    def fmt(r):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(r))
    lines = [fmt(rows[0]), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows[1:]]
    lines.append("")
    lines.append("Per-tile total elapsed_s:")
    for tile, tot in sorted(tiles.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {tile}: {tot:.2f}s")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    as_json = "--json" in argv
    if not args:
        print(__doc__)
        return 2
    records: list[PhaseRecord] = []
    for path in args:
        p = Path(path)
        if not p.exists():
            print(f"warning: {path} not found", file=sys.stderr)
            continue
        with p.open("r", errors="replace") as fh:
            records.extend(parse_phase_lines(fh))
    summaries = summarize(records)
    if as_json:
        print(json.dumps([asdict(s) for s in summaries], indent=2))
    else:
        print(format_table(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
