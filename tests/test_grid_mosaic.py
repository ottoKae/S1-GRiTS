"""Real GDAL VRT/COG outputs from tiny catalog-selected rasters."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds
from rasterio.enums import Resampling

pytest.importorskip('osgeo.gdal')
from s1grits.analysis.mosaic import create_grid_mosaic
from s1grits.canonical_catalog_schema import normalize_catalog_record


def cube(root):
    records, paths = [], []
    for i, tile in enumerate(('17MPU', '17MQU')):
        path = root/tile/'smonthly_ASCENDING/cog/2026-01.tif'
        path.parent.mkdir(parents=True)
        values = np.full((2, 4, 4), 10 if i == 0 else 100, dtype='f4')
        values[1] *= 2
        if i == 0:
            values[0, 1, 2] = -9999
            values[1, 2, 3] = -9999
        transform = from_origin(600000+60*i, 9900000, 30, 30)
        with rasterio.open(path, 'w', driver='COG', width=4, height=4, count=2,
                           dtype='float32', crs='EPSG:32717', transform=transform, nodata=-9999) as dst:
            dst.write(values)
            dst.descriptions = ('VV_dB', 'VH_dB')
        records.append(normalize_catalog_record(dict(
            item_id=f'i{i}', tile_id=tile, product_type='smonthly', output_type='smonthly',
            processing_signature='p_same', crs='EPSG:32717', transform=list(transform)[:6],
            height=4, width=4, bands=['VV_dB', 'VH_dB', 'n_obs'], flight_direction='ASCENDING',
            month='2026-01', datetime='2026-01-01T00:00:00Z', track=18,
            cog_path=path.relative_to(root/tile).as_posix())))
        paths.append(path)
    pd.DataFrame(records).to_parquet(root/'catalog.parquet')
    return records, paths


def call(root, out, **kwargs):
    return create_grid_mosaic(root, kwargs.pop('item_ids', ['i0', 'i1']), out,
        target_crs=kwargs.pop('target_crs', 'EPSG:32717'),
        resolution=kwargs.pop('resolution', 30), bounds=kwargs.pop('bounds', [600000, 9899880, 600210, 9900000]),
        **kwargs)


@pytest.mark.parametrize('format', ['VRT', 'COG'])
def test_values(tmp_path, format):
    root = tmp_path/'cube'
    _, paths = cube(root)
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    out = tmp_path/('out.vrt' if format == 'VRT' else 'out.tif')
    if format == 'COG':
        call(root, out.with_suffix('.vrt'))
    call(root, out, output_format=format)
    expected = np.full((2, 4, 7), np.nan, dtype='f4')
    expected[0, :, :4], expected[1, :, :4] = 10, 20
    expected[0, :, 4:6], expected[1, :, 4:6] = 100, 200
    expected[0, 1, 2], expected[1, 2, 3] = 100, 200
    with rasterio.open(out) as src:
        np.testing.assert_array_equal(src.read(), expected)
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths] == before
    receipt = json.loads(out.with_suffix(out.suffix+'.json').read_text())
    assert receipt['contract']['ordered_item_ids'] == ['i0', 'i1']
    assert receipt['contract']['transform_tolerance_pixels'] == 0
    assert len(list(out.with_name(out.name+'.sources').glob('*.vrt'))) >= 2
    # A fresh process with another working directory can reopen all VRT dependencies.
    proc = subprocess.run([sys.executable, '-c',
        'import rasterio,sys; s=rasterio.open(sys.argv[1]); print(float(s.read(1)[1,2]))', str(out)],
        cwd=tmp_path.parent, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert float(proc.stdout.strip()) == 100
    if format == 'COG':
        moved = tmp_path/'standalone.tif'
        out.rename(moved)
        with rasterio.open(moved) as src:
            np.testing.assert_array_equal(src.read(), expected)


@pytest.mark.parametrize('target', ['EPSG:32717', 'EPSG:32718'])
def test_order_and_cross_zone(tmp_path, target):
    root = tmp_path/'cube'
    _, paths = cube(root)
    with rasterio.open(paths[0]) as src:
        projected = transform_bounds(src.crs, target, 600000, 9899880, 600180, 9900000)
    left, bottom = np.floor(np.array(projected[:2])/30)*30
    right, top = np.ceil(np.array(projected[2:])/30)*30
    bounds = [left, bottom, right, top]
    if target == 'EPSG:32717':
        bounds = [v+15 for v in bounds]  # Explicit half-pixel origin, without -tap snapping.
    out = tmp_path/'cross.vrt'
    call(root, out, target_crs=target, bounds=bounds, item_ids=['i1', 'i0'])
    with rasterio.open(out) as result:
        references = []
        for path in paths[::-1]:
            with rasterio.open(path) as src:
                values = np.full((2, *result.shape), np.nan, dtype='f4')
                for band in (1, 2):
                    reproject(src.read(band), values[band-1], src_crs=src.crs, src_transform=src.transform,
                        src_nodata=src.nodata, dst_crs=result.crs, dst_transform=result.transform,
                        dst_nodata=np.nan, resampling=Resampling.nearest, tolerance=0.0)
                references.append(values)
        expected = np.where(np.isfinite(references[0]), references[0], references[1])
        np.testing.assert_array_equal(result.read(), expected)
        assert np.isfinite(expected).sum() > 0
    assert json.loads(out.with_suffix('.vrt.json').read_text())['contract']['ordered_item_ids'] == ['i1', 'i0']


@pytest.mark.parametrize('failure', ['signature', 'month', 'direction', 'missing', 'bands', 'duplicate_tile', 'partial'])
def test_reject(tmp_path, failure):
    root = tmp_path/'cube'
    records, paths = cube(root)
    if failure == 'signature':
        records[1]['processing_signature'] = 'p_other'
    elif failure == 'month':
        records[1]['month'] = '2026-02'
    elif failure == 'direction':
        records[1]['flight_direction'] = 'DESCENDING'
    elif failure == 'missing':
        paths[1].rename(paths[1].with_suffix('.absent'))
    elif failure == 'bands':
        records[1]['bands'] = '["VH_dB", "VV_dB"]'
    elif failure == 'duplicate_tile':
        records[1]['tile_id'] = records[0]['tile_id']
    elif failure == 'partial':
        records[1]['status'] = 'partial'
    pd.DataFrame(records).to_parquet(root/'catalog.parquet')
    out = tmp_path/'bad.vrt'
    with pytest.raises((ValueError, rasterio.errors.RasterioIOError)):
        call(root, out)
    assert not out.exists() and not (tmp_path/'bad.vrt.sources').exists()


def test_grid_and_overwrite(tmp_path):
    root = tmp_path/'cube'
    cube(root)
    for kwargs in [dict(resolution=0), dict(target_crs='EPSG:4326'),
                   dict(bounds=[600000, 9899880, 600211, 9900000]), dict(item_ids=['absent']),
                   dict(item_ids=['i0', 'i0'])]:
        with pytest.raises(ValueError):
            call(root, tmp_path/'bad.vrt', **kwargs)
    with pytest.raises(ValueError, match='outside'):
        call(root, root/'bad.vrt')
    out = tmp_path/'out.vrt'
    call(root, out)
    before = out.read_bytes()
    with pytest.raises(FileExistsError):
        call(root, out)
    assert out.read_bytes() == before


def test_scene_time_and_cli(tmp_path):
    root = tmp_path/'cube'
    records, _ = cube(root)
    for record in records:
        record['product_type'] = 'scenes'
    records[1]['datetime'] = '2026-01-01T00:00:01Z'
    pd.DataFrame(records).to_parquet(root/'catalog.parquet')
    with pytest.raises(ValueError, match='identical acquisition'):
        call(root, tmp_path/'bad.vrt')
    records[1]['datetime'] = records[0]['datetime']
    pd.DataFrame(records).to_parquet(root/'catalog.parquet')
    items = tmp_path/'items.json'
    items.write_text('["i0", "i1"]')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]/'src'))
    proc = subprocess.run([sys.executable, '-m', 's1grits', 'mosaic_grid',
        '--catalog-root', str(root), '--items-file', str(items), '--output', str(tmp_path/'cli.vrt'),
        '--crs', 'EPSG:32717', '--resolution', '30', '--bounds', '600000', '9899880', '600210', '9900000'],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path/'cli.vrt.json').exists()


def test_large_virtual_extent(tmp_path):
    root = tmp_path/'cube'
    cube(root)
    out = tmp_path/'large.vrt'
    call(root, out, bounds=[0, 6000000, 3999990, 9999990])
    with rasterio.open(out) as src:
        assert src.shape == (133333, 133333)
        assert np.isnan(src.read(window=rasterio.windows.Window(0, 0, 2, 2))).all()
    assert sum(p.stat().st_size for p in tmp_path.rglob('*.vrt')) < 50000
    for dependency in (tmp_path/'large.vrt.sources').glob('*.vrt'):
        with rasterio.open(dependency) as src:
            assert src.width <= 4 and src.height <= 4
