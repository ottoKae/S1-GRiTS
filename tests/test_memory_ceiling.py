"""Memory-ceiling regression test for the blockwise smonthly writer.

The whole point of the blockwise path is that peak memory is bounded by the
*block* working set, never by an ``(n_scenes, H, W)`` full-tile stack.  A future
refactor could silently reintroduce full-tile behaviour (e.g. by falling back
to ``_mosaic_align`` for every scene) without changing output, and only show up
as OOM crashes on a big month.

Rather than assert a noisy absolute RSS number, this test instruments the one
place composites are built (``_monthly_composite_block``) and asserts a
*structural invariant*: no array composited during a deep (100-scene) month
ever has full-tile spatial extent — it stays within one block.  ``ru_maxrss``
is captured and printed as a diagnostic only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from s1grits import workflow_scenes as ws
from s1grits.scenes import smonthly_writer as ws_smw  # noqa: E402
from benchmarks import _synthetic as syn  # noqa: E402


def _peak_rss_mb():
    try:
        import resource
        # ru_maxrss is KiB on Linux, bytes on macOS
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return v / 1024 if sys.platform != "darwin" else v / (1024 * 1024)
    except Exception:
        return None


@pytest.mark.parametrize("num_threads", [1, 4])
def test_blockwise_never_stacks_full_tile(monkeypatch, tmp_path, num_threads):
    height = width = 2048
    chunk = 1024                      # 2x2 = 4 blocks
    block_area = chunk * chunk
    tile_area = height * width
    n_scenes = 100                    # deep month

    fv, pv, fh, ph, idx_by_track, master = syn.build_month(
        height=height, width=width, n_scenes=n_scenes, n_tracks=2,
    )

    observed = {"max_area": 0, "max_depth": 0, "calls": 0}
    real_composite = ws._monthly_composite_block

    def spy_composite(stack, method, trim):
        if stack:
            arr0 = np.asarray(stack[0])
            area = int(arr0.shape[-2]) * int(arr0.shape[-1])
            observed["max_area"] = max(observed["max_area"], area)
            observed["max_depth"] = max(observed["max_depth"], len(stack))
            observed["calls"] += 1
        return real_composite(stack, method, trim)

    monkeypatch.setattr(ws_smw, "_monthly_composite_block", spy_composite)

    g = syn.init_store(tmp_path / "deep.zarr", height, width, chunk)
    res = ws._write_smonthly_month_zarr_blockwise(
        g=g, month_str="2026-01", dt_ns=np.datetime64("2026-01-15", "ns"),
        idx_by_track=idx_by_track, final_vv=fv, prof_vv=pv, final_vh=fh, prof_vh=ph,
        height=height, width=width, transform=master, target_crs="EPSG:32717",
        chunk_y=chunk, chunk_x=chunk,
        band_names=["VV_dB", "VH_dB"], copol_name="VV_dB", crosspol_name="VH_dB",
        features_ratio=False, features_rvi=False, ratio_name="Ratio", rvi_name="RVI",
        composite_method="median", trim_fraction=0.15,
        tile_clip=False, mgrs_tile_id="17MPU", num_threads=num_threads,
    )
    assert res is not None
    assert observed["calls"] > 0, "composite path was never exercised"

    # Structural invariant: composited arrays live in ONE block, never the tile.
    assert observed["max_area"] <= block_area, (
        f"composited array area {observed['max_area']} exceeds block area "
        f"{block_area} — blockwise memory bound violated"
    )
    assert observed["max_area"] < tile_area, "composited a full-tile-sized array"

    # Depth is bounded by scenes overlapping a single block, never all scenes.
    assert observed["max_depth"] < n_scenes, (
        f"composite stack depth {observed['max_depth']} reached the full scene "
        f"count {n_scenes} — footprint pre-filter not applied"
    )

    peak = _peak_rss_mb()
    print(
        f"\n[mem-ceiling] threads={num_threads} scenes={n_scenes} "
        f"max_stack_depth={observed['max_depth']} "
        f"max_block_area_px={observed['max_area']} (tile_px={tile_area}) "
        f"peak_rss_mb={'na' if peak is None else round(peak, 1)}"
    )


def test_deep_month_output_is_thread_invariant(tmp_path):
    """A deep month must produce identical output at 1 vs 4 threads."""
    height = width = 1536
    chunk = 512
    fv, pv, fh, ph, idx_by_track, master = syn.build_month(
        height=height, width=width, n_scenes=100, n_tracks=2,
        scene_h=512, scene_w=1100,
    )
    checksums = []
    for nt in (1, 4):
        g = syn.init_store(tmp_path / f"deep_{nt}.zarr", height, width, chunk)
        res = ws._write_smonthly_month_zarr_blockwise(
            g=g, month_str="2026-01", dt_ns=np.datetime64("2026-01-15", "ns"),
            idx_by_track=idx_by_track, final_vv=fv, prof_vv=pv, final_vh=fh, prof_vh=ph,
            height=height, width=width, transform=master, target_crs="EPSG:32717",
            chunk_y=chunk, chunk_x=chunk,
            band_names=["VV_dB", "VH_dB"], copol_name="VV_dB", crosspol_name="VH_dB",
            features_ratio=False, features_rvi=False, ratio_name="Ratio", rvi_name="RVI",
            composite_method="median", trim_fraction=0.15,
            tile_clip=False, mgrs_tile_id="17MPU", num_threads=nt,
        )
        assert res is not None
        checksums.append((syn.band_checksum(g, "VV_dB"), syn.band_checksum(g, "VH_dB")))
    assert checksums[0] == checksums[1]
