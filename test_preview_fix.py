#!/usr/bin/env python3
"""
Quick verification test for the preview PNG fix.
Tests that the function can handle None ratio parameter.
"""

import sys
from pathlib import Path
import numpy as np
from affine import Affine
import tempfile

# Add S1-GRiTS-V100 to path (the runtime version)
sys.path.insert(0, r"D:\Project\claude-demo\S1-GRiTS-V100\src")

from s1grits.asf_output_writing import _generate_preview_png

print("="*60)
print("Testing Preview PNG Generation Fix")
print("="*60)
print()

# Create sample data
height, width = 500, 500
np.random.seed(42)
vv_db = np.random.uniform(-25, 5, (height, width)).astype(np.float32)
vh_db = np.random.uniform(-30, 0, (height, width)).astype(np.float32)

# Simple UTM transform
src_transform = Affine.translation(500000, 2000000) * Affine.scale(30, -30)
src_crs = "EPSG:32646"

# Test with None ratio (the bug case)
with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
    output_path = tmp.name

try:
    print("Test: Calling _generate_preview_png with ratio=None")
    print(f"  Input: vv_db shape={vv_db.shape}, vh_db shape={vh_db.shape}, ratio=None")
    print()

    result = _generate_preview_png(
        vv_db=vv_db,
        vh_db=vh_db,
        ratio=None,  # This was causing the error
        src_transform=src_transform,
        src_crs=src_crs,
        output_path=output_path,
        preview_res=300.0,
        preview_crs="EPSG:4326",
    )

    # Check result
    output_file = Path(output_path)
    if output_file.exists():
        file_size = output_file.stat().st_size
        print("[SUCCESS] Preview PNG generated without errors!")
        print(f"  Output: {output_path}")
        print(f"  Size: {file_size:,} bytes")
        print(f"  Dimensions: {result['width']} x {result['height']}")
        print()
        print("The fix is working correctly!")
    else:
        print("[FAILED] Output file was not created")
        sys.exit(1)

except AttributeError as e:
    if "NoneType" in str(e):
        print("[FAILED] NoneType error still occurs!")
        print(f"  Error: {e}")
        sys.exit(1)
    raise
except Exception as e:
    print(f"[FAILED] Unexpected error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
finally:
    # Cleanup
    if Path(output_path).exists():
        Path(output_path).unlink()

print("="*60)
print("All tests passed!")
print("="*60)
