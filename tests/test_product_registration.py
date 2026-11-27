"""Local references, rebuilds and sibling derivatives using actual tiny stores."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
import zarr

from s1grits.analysis import derive_cube, register_product
from s1grits.analysis.catalog import resync_catalog_from_filesystem
from s1grits.catalog_registration import REGISTRATION_FILE
from s1grits.catalog_validator import validate_catalog_integrity
from s1grits.resolver import CubeResolver
from test_cube_derivatives import hashes
from test_resolver_selection import product
from test_zarr_group_access import grouped as grouped


@pytest.mark.parametrize('stac_format', [None, 'json', 'geoparquet'])
def test_register_groups_and_rebuild(grouped, tmp_path, stac_format):
    source, rows, bundle = grouped
    before = hashes(source)
    root = tmp_path/'registered'
    for row in rows:
        register_product(root, row, source_root=source)
    assert len(CubeResolver(root).catalog) == 2
    # Rebuild must discard an unregistered, nonexistent asset, not preserve all old rows.
    frame = pd.read_parquet(root/'catalog.parquet')
    ghost = dict(frame.iloc[0], item_id='ghost', zarr_path='missing.zarr')
    pd.concat([frame, pd.DataFrame([ghost])]).to_parquet(root/'catalog.parquet', index=False)
    result = resync_catalog_from_filesystem(root, write_stac=stac_format is not None,
                                          stac_format=stac_format or 'json')
    assert result['success'], result
    r = CubeResolver(root)
    assert set(r.catalog.item_id) == {'a', 'b'}
    for value, item in zip([0, 9], ['a', 'b']):
        row = r.catalog[r.catalog.item_id == item].iloc[0]
        with r.open(row) as ds:
            assert ds.VV_dB[0, 0, 0] == value
        assert r._asset_path(row).resolve() == bundle.resolve()
    tile = CubeResolver(root/'17MPU')
    assert (tile.root/tile.catalog.iloc[0].registration_path).is_file()
    with tile.open(tile.catalog.iloc[1]) as ds:
        assert ds.VV_dB[0, 0, 0] == 9
    assert validate_catalog_integrity(root).valid
    if stac_format == 'json':
        for item in ('a', 'b'):
            path = root/f'17MPU/items/scenes/{item}.json'
            stac = json.loads(path.read_text())
            assert stac['id'] == item
            asset = stac['assets']['zarr']
            assert asset['s1grits:zarr_group'] == f'products/{item}'
            assert (path.parent/asset['href']).resolve() == bundle.resolve()
        assert validate_catalog_integrity(root, strict=True).valid
    if stac_format == 'geoparquet':
        from s1grits.stac_geoparquet_io import read_items_geoparquet
        items = read_items_geoparquet(root/'collections/s1grits-scenes/items.parquet')
        assert {item['assets']['zarr']['s1grits:zarr_group'] for item in items} == {'products/a', 'products/b'}
    assert hashes(source) == before


def test_relative_registration_survives_relocation(grouped, tmp_path):
    source, rows, _ = grouped
    root = tmp_path/'registered'
    registered = register_product(root, rows[1], source_root=source)
    assert not Path(registered['zarr_path']).is_absolute()
    relocated = tmp_path/'relocated'
    shutil.copytree(source, relocated/'catalog')
    shutil.copytree(root, relocated/'registered')
    assert resync_catalog_from_filesystem(relocated/'registered', write_stac=False)['success']
    r = CubeResolver(relocated/'registered')
    row = r.catalog.iloc[0]
    assert r._asset_path(row).resolve().is_relative_to(relocated)
    with r.open(row) as ds:
        assert ds.VV_dB[0, 0, 0] == 9
    assert row.processing_signature == registered['processing_signature']


def test_invalid_registration_preserves_catalog(grouped, tmp_path):
    source, rows, _ = grouped
    root = tmp_path/'registered'
    register_product(root, rows[0], source_root=source)
    before = hashes(root)
    with pytest.raises(ValueError, match='conflicts'):
        register_product(root, dict(rows[1], item_id='a'), source_root=source)
    assert hashes(root) == before
    manifest = root/REGISTRATION_FILE
    data = json.loads(manifest.read_text())
    data['records'][0]['zarr_group'] = 'missing'
    manifest.write_text(json.dumps(data))
    before = hashes(root)
    result = resync_catalog_from_filesystem(root, write_stac=True)
    assert not result['success'], result
    assert 'registration' in result['message']
    assert hashes(root) == before


@pytest.mark.parametrize('change', [{'width': 2}, {'bands': ['missing']}, {'status': 'partial'}])
def test_bad_metadata_rejected_before_output(grouped, tmp_path, change):
    source, rows, _ = grouped
    out = tmp_path/'bad'
    with pytest.raises(ValueError):
        register_product(out, dict(rows[0], **change), source_root=source)
    assert not out.exists()


def test_sibling_products_coexist_and_resync(grouped, tmp_path):
    source, rows, _ = grouped
    root = tmp_path/'collection'
    register_product(root, rows[1], source_root=source)
    static = product(tmp_path/'static', 'static', kind='static', band='angle', value=42)
    group = zarr.open_group(str(tmp_path/'static/17MPU/static.zarr'), mode='r+')
    group.attrs.update(crs=static['crs'], transform=static['transform'])
    register_product(root, static, source_root=tmp_path/'static')
    before = hashes(source)
    args = dict(start='2026-01-01', end='2026-03-01', window=[0, 0, 2, 2], features=[])
    filtered = derive_cube(root, 'b', root/'products/filtered', spatial_filter='tv_bregman', **args)
    monthly = derive_cube(root, 'b', root/'products/monthly', monthly='mean', **args)
    assert len(CubeResolver(root).catalog) == 4
    assert resync_catalog_from_filesystem(root, write_stac=False)['success']
    r = CubeResolver(root)
    assert set(r.catalog.item_id) == {'b', 'static', filtered['item_id'], monthly['item_id']}
    for record, count in [(filtered, 4), (monthly, 3)]:
        row = r.catalog[r.catalog.item_id == record['item_id']].iloc[0]
        with r.open(row) as ds:
            assert ds.sizes['time'] == count
            expected = np.full((2, 2), 9.)
            if record is monthly:
                expected[0, 1] = np.nan  # Monthly validity requires finite VV and VH.
                assert np.isnan(ds.VV_dB.isel(time=1)).all()
                assert (ds.n_obs.isel(time=1) == 0).all()
            np.testing.assert_allclose(ds.VV_dB.isel(time=0), expected, atol=1e-5)
    with r.open(r.catalog[r.catalog.item_id == 'static'].iloc[0]) as ds:
        np.testing.assert_array_equal(ds.angle, np.full((2, 2), 42))
    assert hashes(source) == before
    with pytest.raises(FileExistsError):
        derive_cube(root, 'b', root/'products/filtered', **args)


def test_registration_cli(grouped, tmp_path):
    source, rows, _ = grouped
    path = tmp_path/'record.json'
    path.write_text(json.dumps(rows[0]), encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 's1grits', 'catalog', 'register',
        '--output-dir', str(tmp_path/'cli'), '--record', str(path), '--source-root', str(source)],
        env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    assert CubeResolver(tmp_path/'cli').catalog.iloc[0].item_id == 'a'


def test_nested_output_cannot_enter_another_store(grouped, tmp_path):
    source, rows, _ = grouped
    root = tmp_path/'collection'
    register_product(root, rows[0], source_root=source)
    other = root/'other.zarr'
    zarr.open_group(str(other), mode='w')
    before = hashes(root)
    with pytest.raises(ValueError, match='existing Zarr stores'):
        derive_cube(root, 'a', other/'inside', start='2026-01-01', end='2026-01-01')
    assert hashes(root) == before


def test_same_asset_explicit_items_are_retained(grouped, tmp_path):
    source, rows, _ = grouped
    root = tmp_path/'registered'
    register_product(root, rows[0], source_root=source)
    register_product(root, dict(rows[0], item_id='alias'), source_root=source)
    assert set(CubeResolver(root).catalog.item_id) == {'a', 'alias'}
    assert resync_catalog_from_filesystem(root, write_stac=False)['success']
    assert set(CubeResolver(root).catalog.item_id) == {'a', 'alias'}


def test_legacy_store_cannot_have_mixed_declared_signatures(grouped, tmp_path):
    source, rows, bundle = grouped
    group = zarr.open_group(str(bundle), path='products/a', mode='r+')
    del group.attrs['processing_signature']
    root = tmp_path/'registered'
    register_product(root, rows[0], source_root=source)
    before = hashes(root)
    with pytest.raises(ValueError, match='multiple processing signatures'):
        register_product(root, dict(rows[0], item_id='other', processing_signature='p_other'), source_root=source)
    assert hashes(root) == before


def test_register_resolver_catalog_row(grouped, tmp_path):
    source, _, _ = grouped
    row = CubeResolver(source).catalog.iloc[0]
    result = register_product(tmp_path/'registered', row, source_root=source)
    assert result['item_id'] == row.item_id
    assert result['transform'] == list(row['transform'])


@pytest.mark.parametrize('stac_format', [None, 'json', 'geoparquet'])
def test_resync_does_not_treat_root_store_as_tile(grouped, stac_format):
    root, rows, bundle = grouped
    group = zarr.open_group(str(bundle), mode='r+')
    group.create_group('items').create_array('values', data=np.array([1, 2, 3]))
    for row in rows:
        register_product(root, row)
    before = hashes(bundle)
    assert resync_catalog_from_filesystem(root, write_stac=stac_format is not None,
                                         stac_format=stac_format or 'json')['success']
    assert hashes(bundle) == before
    assert set(CubeResolver(root).catalog.item_id) == {'a', 'b'}
