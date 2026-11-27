"""Create a tiny synthetic Cube for the existing-Cube tutorial; no downloads."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import zarr

from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.zarr_encoding import create_zarr_array


def create_demo_cube(output: Path) -> Path:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tile = output / '17MPU'
    path = tile / 'scenes_ASCENDING/zarr/demo.zarr'
    group = zarr.open_group(str(path), mode='w', zarr_format=3)
    transform = [30, 0, 600000, 0, -30, 9900000]
    group.attrs.update(crs='EPSG:32717', transform=transform,
                       sample_kind='synthetic', generation_id='demo-v1')
    times = np.array(['2026-01-01', '2026-01-13', '2026-01-25', '2026-03-01'],
                     dtype='datetime64[ns]')
    power = np.broadcast_to(np.array([1, 2, 9, 4], dtype='f4')[:, None, None], (4, 4, 5)).copy()
    power[:, 0, 0] = np.nan
    power[1, 1, 1] = np.nan
    arrays = {
        'x': (600000 + (np.arange(5) + .5) * 30, ['x']),
        'y': (9900000 - (np.arange(4) + .5) * 30, ['y']),
        'time': (times, ['time']),
        'VV_dB': (10 * np.log10(power), ['time', 'y', 'x']),
        'VH_dB': (10 * np.log10(power * .25), ['time', 'y', 'x']),
    }
    for name, (values, dims) in arrays.items():
        array = create_zarr_array(group, name, data=values, dimension_names=dims)
        array.attrs['_ARRAY_DIMENSIONS'] = dims
    record = normalize_catalog_record(dict(
        item_id='demo-scenes', tile_id='17MPU', product_type='scenes', output_type='scenes',
        flight_direction='ASCENDING', track=18, geometry_group_id='17MPU_ASCENDING_TK18',
        processing_signature='p_synthetic_demo_v1', processing_level='synthetic',
        crs='EPSG:32717', transform=transform, width=5, height=4,
        datetime=str(times[0]), start_datetime=str(times[0]), end_datetime=str(times[-1]),
        bands=['VV_dB', 'VH_dB'], zarr_path=path.relative_to(tile).as_posix()))
    catalog = pd.DataFrame([record])
    catalog.to_parquet(output / 'catalog.parquet')
    catalog.to_parquet(tile / 'catalog.parquet')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='New, nonexistent Cube root')
    args = parser.parse_args()
    print(f'Synthetic Cube created: {create_demo_cube(args.output)} (item-id: demo-scenes)')
