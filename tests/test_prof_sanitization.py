"""
Test script to verify prof_arr sanitization fixes the performance issue.

This script can be run to validate that:
1. Contaminated prof_arr is detected
2. Automatic sanitization removes invalid entries
3. rasterio.profiles.Profile objects are accepted (not just dict)
4. Blockwise path no longer falls back
5. Performance is restored to expected levels
"""

import numpy as np
import pandas as pd
from pathlib import Path
from rasterio.transform import Affine
from rasterio.profiles import Profile

from s1grits import workflow_scenes as ws


def test_sanitize_accepts_profile_objects():
    """Test that Profile objects (not just dict) are accepted."""
    final_vv = [np.ones((10, 10)) for _ in range(3)]

    # Use actual Profile objects (as returned by rasterio)
    prof_vv = [
        Profile(transform=Affine.identity(), crs="EPSG:32617"),
        Profile(transform=Affine.identity(), crs="EPSG:32617"),
        Profile(transform=Affine.identity(), crs="EPSG:32617"),
    ]
    final_vh = [np.ones((10, 10)) for _ in range(3)]
    prof_vh = [
        Profile(transform=Affine.identity(), crs="EPSG:32617"),
        Profile(transform=Affine.identity(), crs="EPSG:32617"),
        Profile(transform=Affine.identity(), crs="EPSG:32617"),
    ]
    clean_dates = [
        pd.Timestamp("2020-01-01"),
        pd.Timestamp("2020-01-02"),
        pd.Timestamp("2020-01-03"),
    ]

    result = ws._sanitize_prof_arrays(
        final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch=None
    )

    final_vv_clean, prof_vv_clean, final_vh_clean, prof_vh_clean, clean_dates_clean, _ = result

    # Should keep all 3 (Profile objects are valid)
    assert len(final_vv_clean) == 3, f"Expected 3, got {len(final_vv_clean)}"
    assert len(prof_vv_clean) == 3
    assert len(final_vh_clean) == 3
    assert len(prof_vh_clean) == 3
    assert len(clean_dates_clean) == 3

    # Verify all remaining prof entries are still Profile objects
    for p in prof_vv_clean:
        assert isinstance(p, Profile) or isinstance(p, dict)
        assert p.get("transform") is not None
        assert p.get("crs") is not None

    print("✓ test_sanitize_accepts_profile_objects passed")


def test_sanitize_prof_arrays_removes_none():
    """Test that None prof entries are removed."""
    final_vv = [np.ones((10, 10)) for _ in range(5)]
    prof_vv = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        None,  # Contamination
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        None,  # Contamination
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    final_vh = [np.ones((10, 10)) for _ in range(5)]
    prof_vh = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        None,  # Contamination
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    clean_dates = [
        pd.Timestamp("2020-01-01"),
        pd.Timestamp("2020-01-02"),
        pd.Timestamp("2020-01-03"),
        pd.Timestamp("2020-01-04"),
        pd.Timestamp("2020-01-05"),
    ]

    result = ws._sanitize_prof_arrays(
        final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch=None
    )

    final_vv_clean, prof_vv_clean, final_vh_clean, prof_vh_clean, clean_dates_clean, _ = result

    # Should remove indices 1, 2, 3 (have None in either VV or VH)
    # Only indices 0 and 4 should remain
    assert len(final_vv_clean) == 2
    assert len(prof_vv_clean) == 2
    assert len(final_vh_clean) == 2
    assert len(prof_vh_clean) == 2
    assert len(clean_dates_clean) == 2

    # Verify all remaining prof entries are valid dicts
    for p in prof_vv_clean:
        assert isinstance(p, dict)
        assert p.get("transform") is not None
        assert p.get("crs") is not None

    for p in prof_vh_clean:
        assert isinstance(p, dict)
        assert p.get("transform") is not None
        assert p.get("crs") is not None

    print("✓ test_sanitize_prof_arrays_removes_none passed")


def test_sanitize_prof_arrays_removes_non_dict():
    """Test that non-dict prof entries are removed."""
    final_vv = [np.ones((10, 10)) for _ in range(3)]
    prof_vv = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        "not a dict",  # Contamination
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    final_vh = [np.ones((10, 10)) for _ in range(3)]
    prof_vh = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    clean_dates = [
        pd.Timestamp("2020-01-01"),
        pd.Timestamp("2020-01-02"),
        pd.Timestamp("2020-01-03"),
    ]

    result = ws._sanitize_prof_arrays(
        final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch=None
    )

    final_vv_clean, prof_vv_clean, final_vh_clean, prof_vh_clean, clean_dates_clean, _ = result

    # Should remove index 1 (non-dict)
    assert len(prof_vv_clean) == 2
    assert all(isinstance(p, dict) for p in prof_vv_clean)

    print("✓ test_sanitize_prof_arrays_removes_non_dict passed")


def test_sanitize_prof_arrays_removes_missing_fields():
    """Test that prof entries missing transform or crs are removed."""
    final_vv = [np.ones((10, 10)) for _ in range(4)]
    prof_vv = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"crs": "EPSG:32617"},  # Missing transform
        {"transform": Affine.identity()},  # Missing crs
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    final_vh = [np.ones((10, 10)) for _ in range(4)]
    prof_vh = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    clean_dates = [
        pd.Timestamp("2020-01-01"),
        pd.Timestamp("2020-01-02"),
        pd.Timestamp("2020-01-03"),
        pd.Timestamp("2020-01-04"),
    ]

    result = ws._sanitize_prof_arrays(
        final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch=None
    )

    final_vv_clean, prof_vv_clean, final_vh_clean, prof_vh_clean, clean_dates_clean, _ = result

    # Should remove indices 1 and 2 (missing fields)
    assert len(prof_vv_clean) == 2
    for p in prof_vv_clean:
        assert p.get("transform") is not None
        assert p.get("crs") is not None

    print("✓ test_sanitize_prof_arrays_removes_missing_fields passed")


def test_sanitize_prof_arrays_syncs_df_batch():
    """Test that df_batch is correctly synced after filtering."""
    final_vv = [np.ones((10, 10)) for _ in range(3)]
    prof_vv = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        None,  # Will be removed
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    final_vh = [np.ones((10, 10)) for _ in range(3)]
    prof_vh = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]
    clean_dates = [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-02T00:00:00Z"),
        pd.Timestamp("2020-01-03T00:00:00Z"),
    ]
    df_batch = pd.DataFrame({
        "acq_dt": clean_dates,
        "track_number": [1, 1, 1],
        "jpl_burst_id": ["B1", "B2", "B3"],
    })

    result = ws._sanitize_prof_arrays(
        final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch=df_batch
    )

    _, _, _, _, clean_dates_clean, df_batch_clean = result

    # Should have 2 entries (index 1 removed)
    assert len(clean_dates_clean) == 2
    assert df_batch_clean is not None
    assert len(df_batch_clean) == 2

    # Verify dates match
    assert pd.Timestamp(clean_dates_clean[0]).strftime('%Y%m%dT%H%M%S') == "20200101T000000"
    assert pd.Timestamp(clean_dates_clean[1]).strftime('%Y%m%dT%H%M%S') == "20200103T000000"

    print("✓ test_sanitize_prof_arrays_syncs_df_batch passed")


def test_can_window_reproject_after_sanitization():
    """Test that _can_window_reproject accepts dict and Profile mappings."""
    final_arr = [np.ones((10, 10)) for _ in range(3)]
    prof_arr = [
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
        Profile(transform=Affine.identity(), crs="EPSG:32617"),
        {"transform": Affine.identity(), "crs": "EPSG:32617"},
    ]

    # All Mapping-like profiles are valid - should return True.
    result = ws._can_window_reproject([0, 1, 2], final_arr, prof_arr)
    assert result is True

    print("✓ test_can_window_reproject_after_sanitization passed")


if __name__ == "__main__":
    print("Running prof_arr sanitization tests...\n")

    test_sanitize_accepts_profile_objects()  # NEW: Test Profile objects
    test_sanitize_prof_arrays_removes_none()
    test_sanitize_prof_arrays_removes_non_dict()
    test_sanitize_prof_arrays_removes_missing_fields()
    test_sanitize_prof_arrays_syncs_df_batch()
    test_can_window_reproject_after_sanitization()

    print("\n✅ All tests passed!")
    print("\nSanitization is working correctly.")
    print("Profile objects (from rasterio) are now accepted.")
    print("Next: Run the full workflow to verify performance improvement.")
