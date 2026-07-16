"""
Display mosaic enhancement module

Provides per-tile percentile normalization for large-scale mosaic visualization
without modifying the analysis-ready data products.

Each source tile is normalized independently to [0, 1] using its own p2/p98
statistics, then merged into a display VRT via gdalbuildvrt. Memory usage is
proportional to the size of a single tile, not the entire mosaic.

The display VRT is separate from the data mosaic and should only be used for
visualization purposes, not for analysis.
"""

import subprocess
import tempfile
import defusedxml.ElementTree as ET
import numpy as np
import rioxarray
from pathlib import Path
from typing import Optional


def create_display_vrt(
    data_mosaic_vrt: str,
    output_path: str,
    percentile_min: float = 2,
    percentile_max: float = 98,
    nodata: float = -9999
) -> Optional[str]:
    """
    Create a display-optimized VRT by normalizing each source tile individually.

    Parses the source tile list from the input mosaic VRT, then applies
    per-band percentile normalization to each tile independently. Tiles are
    processed one at a time so peak memory usage equals a single tile
    (~200 MB), not the full mosaic.

    Args:
        data_mosaic_vrt: Path to input data mosaic VRT (from mosaic create)
        output_path: Output path for display VRT (must end in .vrt)
        percentile_min: Lower percentile for clipping (default: 2)
        percentile_max: Upper percentile for clipping (default: 98)
        nodata: NoData sentinel value in source tiles (default: -9999)

    Returns:
        Path to created display VRT, or None if an error occurred

    Example:
        >>> display_vrt = create_display_vrt(
        ...     'mosaic.vrt',
        ...     'mosaic_display.vrt',
        ...     percentile_min=2,
        ...     percentile_max=98
        ... )
    """
    print("=" * 70)
    print("Creating Display Mosaic (per-tile normalization)")
    print("=" * 70)
    print(f"Input:       {Path(data_mosaic_vrt).name}")
    print(f"Output:      {Path(output_path).name}")
    print(f"Percentiles: p{percentile_min} - p{percentile_max}")

    # --- Step 1: parse VRT XML to get unique source tile paths ---------------
    try:
        tree = ET.parse(data_mosaic_vrt)
    except ET.ParseError as exc:
        print(f"ERROR: Failed to parse VRT XML: {exc}")
        return None

    vrt_dir = Path(data_mosaic_vrt).parent.resolve()
    seen: set = set()
    source_files: list = []

    for elem in tree.getroot().findall(".//SourceFilename"):
        path_str = elem.text
        if not path_str:
            continue
        # relativeToVRT="1" means path is relative to the VRT file's directory
        if elem.get("relativeToVRT", "0") == "1":
            abs_path = str((vrt_dir / path_str).resolve())
        else:
            abs_path = str(Path(path_str).resolve())

        if abs_path not in seen:
            seen.add(abs_path)
            source_files.append(abs_path)

    if not source_files:
        print("ERROR: No source files found in VRT")
        return None

    print(f"Source tiles: {len(source_files)}")

    # --- Step 2: normalize each tile independently ---------------------------
    temp_dir = Path(output_path).parent / f".tmp_display_{Path(output_path).stem}"
    temp_dir.mkdir(exist_ok=True)

    normalized_files: list = []

    for i, tile_file in enumerate(source_files):
        print(f"  [{i + 1:>3}/{len(source_files)}] {Path(tile_file).name}")

        try:
            ds = rioxarray.open_rasterio(tile_file, masked=False)
        except Exception as exc:
            print(f"  WARN: Cannot open {tile_file}: {exc} — skipping")
            continue

        # (bands, height, width) float32
        data = ds.values.astype(np.float32)
        output_data = np.full_like(data, nodata, dtype=np.float32)

        for band_idx in range(data.shape[0]):
            band = data[band_idx]
            valid = band[band != nodata]

            if len(valid) == 0:
                # All nodata — leave this band as nodata in output
                continue

            vmin = float(np.percentile(valid, percentile_min))
            vmax = float(np.percentile(valid, percentile_max))
            if vmax <= vmin:
                vmax = vmin + 1.0  # prevent division by zero for constant tiles

            output_data[band_idx] = np.where(
                band != nodata,
                np.clip((band - vmin) / (vmax - vmin), 0.0, 1.0),
                nodata,
            ).astype(np.float32)

        out_tif = str(temp_dir / f"tile_{i:04d}.tif")
        try:
            ds.copy(data=output_data).rio.to_raster(
                out_tif,
                driver="GTiff",
                compress="DEFLATE",
                nodata=nodata,
            )
        except Exception as exc:
            print(f"  WARN: Failed to write normalized tile: {exc} — skipping")
            continue

        normalized_files.append(str(Path(out_tif).resolve().as_posix()))

    if not normalized_files:
        print("ERROR: No tiles were normalized successfully")
        return None

    # --- Step 3: merge normalized tiles into display VRT ---------------------
    abs_output_vrt = str(Path(output_path).resolve())
    filelist_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            f.write("\n".join(normalized_files))
            filelist_path = f.name

        subprocess.run(
            [
                "gdalbuildvrt",
                "-srcnodata", str(nodata),
                "-input_file_list", filelist_path,
                abs_output_vrt,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: gdalbuildvrt failed: {exc.stderr}")
        return None
    finally:
        if filelist_path:
            Path(filelist_path).unlink(missing_ok=True)

    print("\nSUCCESS: Display mosaic created")
    print(f"Output: {output_path}")
    print("=" * 70)

    return output_path
