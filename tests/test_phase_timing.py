"""Deterministic unit tests for the phase-timing log parser.

These validate ``benchmarks/phase_timing.py`` against real ``[PHASE]`` log
lines emitted by ``workflow_scenes._phase_timer`` (formats copied verbatim
from a production run), plus malformed-input tolerance.  Pure parsing — no
workflow import, no I/O.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo-root importable so ``benchmarks`` is a package here.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from benchmarks import phase_timing as pt  # noqa: E402


# Verbatim lines from a real run (workflow_scenes _phase_timer output).
REAL_LINES = [
    "2026-07-05 00:25:02 - s1_processor.s1grits.workflow_scenes - INFO - "
    "[PHASE] metadata.query START tile=17MPU rss_mb=156.8",
    "2026-07-05 00:25:16 - s1_processor.s1grits.workflow_scenes - INFO - "
    "[PHASE] metadata.query END elapsed_s=14.70 rss_mb=321.3 delta_mb=164.5 tile=17MPU rows=144",
    "2026-07-05 00:25:16 - s1_processor.s1grits.workflow_scenes - INFO - "
    "[PHASE] download.strict_vv_vh START tile=17MPU batch=1/1 scenes=80 rss_mb=321.3",
    "2026-07-05 00:26:20 - s1_processor.s1grits.workflow_scenes - INFO - "
    "[PHASE] download.strict_vv_vh END elapsed_s=63.45 rss_mb=6837.9 delta_mb=6516.6 "
    "tile=17MPU batch=1/1 scenes=80",
    "2026-07-05 00:26:57 - s1_processor.s1grits.workflow_scenes - INFO - "
    "[PHASE] write.smonthly START tile=17MPU batch=1/1 scenes=80 rss_mb=6793.2",
    "2026-07-05 01:07:37 - s1_processor.s1grits.workflow_scenes - INFO - "
    "[PHASE] write.smonthly END elapsed_s=2440.77 rss_mb=6858.1 delta_mb=64.9 "
    "tile=17MPU batch=1/1 scenes=80",
]


def test_parses_start_and_end_events():
    recs = pt.parse_phase_lines(REAL_LINES)
    assert len(recs) == 6
    starts = [r for r in recs if r.event == "START"]
    ends = [r for r in recs if r.event == "END"]
    assert len(starts) == 3 and len(ends) == 3


def test_numeric_and_context_fields_typed():
    recs = pt.parse_phase_lines(REAL_LINES)
    q_end = next(r for r in recs if r.name == "metadata.query" and r.event == "END")
    assert q_end.elapsed_s == 14.70
    assert q_end.rss_mb == 321.3
    assert q_end.fields["delta_mb"] == 164.5
    assert q_end.fields["rows"] == 144.0  # numeric field coerced
    assert q_end.tile == "17MPU"  # context field stays string


def test_write_phase_captures_long_elapsed():
    recs = pt.parse_phase_lines(REAL_LINES)
    w = next(r for r in recs if r.name == "write.smonthly" and r.event == "END")
    assert w.elapsed_s == 2440.77
    assert w.fields["batch"] == "1/1"


def test_summarize_aggregates_by_tile_phase():
    summaries = pt.summarize(pt.parse_phase_lines(REAL_LINES))
    # Only END events contribute; three phases for one tile.
    phases = {s.phase: s for s in summaries}
    assert set(phases) == {"metadata.query", "download.strict_vv_vh", "write.smonthly"}
    assert phases["write.smonthly"].elapsed_s == 2440.77
    assert phases["download.strict_vv_vh"].peak_rss_mb == 6837.9
    # Sorted so the most expensive phase is first for the tile.
    assert summaries[0].phase == "write.smonthly"


def test_summarize_sums_batches_of_same_phase():
    lines = [
        "x [PHASE] write.smonthly END elapsed_s=10.0 rss_mb=100.0 delta_mb=1.0 tile=T batch=1/2",
        "x [PHASE] write.smonthly END elapsed_s=5.0 rss_mb=120.0 delta_mb=2.0 tile=T batch=2/2",
    ]
    s = pt.summarize(pt.parse_phase_lines(lines))
    assert len(s) == 1
    assert s[0].elapsed_s == 15.0          # summed
    assert s[0].peak_rss_mb == 120.0        # max
    assert s[0].max_delta_mb == 2.0
    assert s[0].count == 2


def test_handles_na_rss_and_failed_event():
    lines = [
        "x [PHASE] qc.acquisitions START tile=T rss_mb=na",
        "x [PHASE] qc.acquisitions FAILED elapsed_s=3.14 rss_mb=na tile=T",
    ]
    recs = pt.parse_phase_lines(lines)
    failed = next(r for r in recs if r.event == "FAILED")
    assert failed.rss_mb is None            # 'na' -> not a number
    assert failed.elapsed_s == 3.14
    s = pt.summarize(recs)
    assert s[0].failed == 1
    assert s[0].peak_rss_mb is None


def test_ignores_non_phase_and_malformed_lines():
    lines = [
        "",
        "2026-07-05 - some unrelated INFO line",
        "[PHASE] incomplete_line_without_event tile=T",
        "garbage [PHASE]",
        "x [PHASE] good.phase END elapsed_s=1.0 rss_mb=5.0 tile=T",
    ]
    recs = pt.parse_phase_lines(lines)
    assert len(recs) == 1
    assert recs[0].name == "good.phase"


def test_format_table_smoke_and_empty():
    assert "no [PHASE]" in pt.format_table([])
    table = pt.format_table(pt.summarize(pt.parse_phase_lines(REAL_LINES)))
    assert "write.smonthly" in table
    assert "17MPU" in table
    assert "Per-tile total" in table
