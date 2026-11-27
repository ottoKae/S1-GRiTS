"""Small real-file views: lazy selection, numerical parity and explicit writes."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from dask.callbacks import Callback
import numpy as np
import pytest
import zarr

from s1grits.analysis import define_view, register_view, materialize_view, derive_cube
from s1grits.analysis.catalog import resync_catalog_from_filesystem
from s1grits.catalog_validator import validate_catalog_integrity
from s1grits.resolver import CubeResolver
from s1grits.zarr_encoding import create_zarr_array
from test_cube_derivatives import source, hashes
from test_zarr_group_access import grouped as grouped


def sample(root, layout='tyx', masked=False):
    group, vv, vh = source(root, layout)
    group.attrs['generation_id'] = 'g1'
    vh[0, 1, 1] = -9999
    group['VH_dB'][:] = vh if layout == 'tyx' else vh.transpose(1, 2, 0)
    if masked:
        for prefix, values in [('VV', vv), ('VH', vh)]:
            valid = np.isfinite(values) & (values != -12345) & (values != -9999)
            interpolated = np.zeros(values.shape, dtype='u1')
            valid[2, 0, 0], interpolated[2, 0, 0] = False, 1
            for kind, array in [('observed', valid.astype('u1')), ('interpolated', interpolated)]:
                dims = ['time', 'y', 'x'] if layout == 'tyx' else ['y', 'x', 'time']
                var = create_zarr_array(group, f'{prefix}_{kind}_mask',
                    data=array if layout == 'tyx' else array.transpose(1, 2, 0), dimension_names=dims)
                var.attrs['_ARRAY_DIMENSIONS'] = dims
    return group


OPTIONS = dict(start='2026-01-01', end='2026-03-01', window=[0, 0, 2, 2])


@pytest.mark.parametrize('layout,signature', [('tyx', 'p79faa1ac5eb'), ('yxt', 'p11137562a44')])
def test_static_variable_order_does_not_change_parent_evidence(tmp_path, layout, signature):
    from s1grits.analysis.views import _describe
    group = sample(tmp_path/'p', layout)
    definition = define_view(tmp_path/'p', 'parent', bands=['VV_dB'], **OPTIONS)
    # Frozen from the pre-fix implementation for both supported time-axis layouts.
    assert definition['processing_signature'] == signature
    create_zarr_array(group, 'angle', data=np.full((24, 30), 45, dtype='f4'), dimension_names=['y', 'x'])
    resolver = CubeResolver(tmp_path/'p')
    with resolver.open(definition['parent']) as ds:
        for names in [['angle', 'VV_dB', 'VH_dB'], ['VV_dB', 'VH_dB', 'angle']]:
            contract = _describe(definition['parent'], group, ds[names], bands=['VV_dB'], **OPTIONS)
            assert contract == definition['contract']
    record = register_view(tmp_path/'v', definition)
    with CubeResolver(tmp_path/'v').open(record) as ds:
        assert ds.VV_dB.isel(time=0, y=0, x=0).compute().item() == 0


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
@pytest.mark.parametrize('masked', [False, True])
@pytest.mark.parametrize('bands', [['VV_dB'], ['Ratio'], ['RVI'], ['VV_dB', 'VH_dB', 'Ratio', 'RVI']])
def test_view_matches_derivative_and_materialization(tmp_path_factory, layout, masked, bands):
    root = tmp_path_factory.mktemp('v')
    parent = root/'p'
    sample(parent, layout, masked)
    before = hashes(parent)
    executed = []
    with Callback(posttask=lambda *args: executed.append(args[0])):
        definition = define_view(parent, 'parent', bands=bands, **OPTIONS)
        assert set(root.iterdir()) == {parent}  # Definition alone is in-memory metadata.
        record = register_view(root/'c', definition)
        assert not list((root/'c').rglob('*.zarr'))
        r = CubeResolver(root/'c')
        with r.open(record) as virtual:
            assert hasattr(virtual[bands[0]].data, 'dask')
            assert not executed
            actual = virtual.compute()
    assert executed
    expected_record = derive_cube(parent, 'parent', root/'e', **OPTIONS)
    saved = materialize_view(root/'c', record['item_id'], root/'c/m', chunk_size=2)
    with CubeResolver(root/'e').open(expected_record) as expected, CubeResolver(root/'c/m').open(saved) as physical:
        assert set(actual.data_vars) == set(bands) | ({f'{p}_{k}_mask' for p in ['VV', 'VH']
                                                    for k in ['observed', 'interpolated']} if masked else set())
        for name in list(actual.data_vars)+['time', 'x', 'y']:
            np.testing.assert_array_equal(actual[name], expected[name])
            np.testing.assert_array_equal(actual[name], physical[name])
        assert physical.attrs['processing_signature'] == record['processing_signature']
        assert saved['processing_level'] == 'derived'
    assert len(CubeResolver(root/'c/m').query(time_start='2026-01-01', time_end='2026-03-01')) == 1
    assert hashes(parent) == before
    assert len(CubeResolver(root/'c').catalog) == 2


@pytest.mark.parametrize('stac_format', [None, 'json', 'geoparquet'])
def test_views_survive_resync_and_stac(tmp_path, stac_format):
    sample(tmp_path/'p')
    row = register_view(tmp_path/'c', define_view(tmp_path/'p', 'parent', **OPTIONS))
    result = resync_catalog_from_filesystem(tmp_path/'c', write_stac=stac_format is not None,
                                          stac_format=stac_format or 'json')
    assert result['success'], result
    r = CubeResolver(tmp_path/'c')
    assert r.catalog.iloc[0].view_path == row['view_path']
    with r.open(r.catalog.iloc[0]) as ds:
        assert ds.VV_dB[0, 0, 0].compute().item() == 0
    check = validate_catalog_integrity(tmp_path/'c', strict=stac_format == 'json')
    assert check.valid, check.report()
    if stac_format == 'json':
        path = tmp_path/'c/17MPU'/row['item_path']
        stac = json.loads(path.read_text())
        assert stac['properties']['s1grits:representation'] == 'virtual'
        assert 'zarr' not in stac['assets']
        assert (path.parent/stac['assets']['view']['href']).is_file()
    elif stac_format == 'geoparquet':
        from s1grits.stac_geoparquet_io import read_items_geoparquet
        path = tmp_path/'c/collections/s1grits-scenes/items.parquet'
        items = read_items_geoparquet(path)
        assert len(items) == 1
        assert items[0]['properties']['s1grits:representation'] == 'virtual'
        assert 'zarr' not in items[0]['assets']
        assert (path.parent/items[0]['assets']['view']['href']).is_file()


def test_generation_change_rejects_view_but_preserves_saved_asset(grouped, tmp_path_factory):
    source_root, _, bundle = grouped
    root = tmp_path_factory.mktemp('g')
    definition = define_view(source_root, 'b', **OPTIONS)
    row = register_view(root/'c', definition)
    saved = materialize_view(root/'c', row['item_id'], root/'m')
    child = zarr.open_group(str(bundle), path='products/b', mode='r+')
    child.attrs['generation_id'] = 'g2'
    child['VV_dB'][:] = 3
    with pytest.raises(ValueError, match='Parent version'):
        CubeResolver(root/'c').open(row)
    before = hashes(root/'c')
    assert not resync_catalog_from_filesystem(root/'c', write_stac=False)['success']
    assert hashes(root/'c') == before
    with CubeResolver(root/'m').open(saved) as ds:
        np.testing.assert_array_equal(ds.VV_dB, np.full((4, 2, 2), 9))


def test_missing_generation_is_explicit(tmp_path):
    group = sample(tmp_path/'p')
    del group.attrs['generation_id']
    definition = define_view(tmp_path/'p', 'parent', **OPTIONS)
    assert definition['contract']['parent_version_evidence'] == 'metadata_only'
    row = register_view(tmp_path/'c', definition)
    with pytest.warns(UserWarning, match='no generation_id'):
        with CubeResolver(tmp_path/'c').open(row) as ds:
            assert ds.attrs['parent_version_evidence'] == 'metadata_only'


def test_move_definitions_and_parents_without_changing_identity(tmp_path):
    sample(tmp_path/'old/p')
    row = register_view(tmp_path/'old/c', define_view(tmp_path/'old/p', 'parent', **OPTIONS))
    shutil.copytree(tmp_path/'old', tmp_path/'new')
    assert resync_catalog_from_filesystem(tmp_path/'new/c', write_stac=False)['success']
    r = CubeResolver(tmp_path/'new/c')
    with r.open(r.catalog.iloc[0]) as ds:
        assert ds.attrs['processing_signature'] == row['processing_signature']
        assert Path(ds.attrs['view_definition']['parent']['zarr_path']).is_relative_to(tmp_path/'new')
        assert ds.VV_dB[0, 0, 0].compute().item() == 0


def test_failed_computation_is_not_published(tmp_path_factory):
    root = tmp_path_factory.mktemp('f')
    group = sample(root/'p', masked=True)
    group['VV_observed_mask'][0, 0, 0] = 2
    row = register_view(root/'c', define_view(root/'p', 'parent', **OPTIONS))
    before = hashes(root/'c')
    with pytest.raises(ValueError, match='only 0 or 1'):
        materialize_view(root/'c', row['item_id'], root/'failed', chunk_size=2)
    assert not (root/'failed/catalog.parquet').exists()
    stores = list((root/'failed').rglob('*.zarr'))
    assert len(stores) == 1
    assert zarr.open_group(str(stores[0]), mode='r').attrs['status'] == 'failed'
    assert hashes(root/'c') == before


def test_invalid_definitions_and_nested_views_are_rejected(tmp_path):
    sample(tmp_path/'p')
    definition = define_view(tmp_path/'p', 'parent', **OPTIONS)
    bad = copy.deepcopy(definition)
    bad['contract']['bands'] = ['unknown']
    with pytest.raises(ValueError, match='scientific signature'):
        register_view(tmp_path/'bad', bad)
    assert not (tmp_path/'bad').exists()
    row = register_view(tmp_path/'c', definition)
    with pytest.raises(ValueError, match='nested views'):
        define_view(tmp_path/'c', row['item_id'], **OPTIONS)
    with pytest.raises(ValueError, match='materialize_view'):
        CubeResolver(tmp_path/'c').materialize_training_cube('17MPU', tmp_path/'training')
    assert not (tmp_path/'training').exists()


def test_view_cli(tmp_path_factory):
    root = tmp_path_factory.mktemp('cli')
    sample(root/'p')
    common = [sys.executable, '-m', 's1grits', 'view']
    result = subprocess.run(common+['create', '--catalog-root', str(root/'p'), '--item-id', 'parent',
        '--output', str(root/'c'), '--start', OPTIONS['start'], '--end', OPTIONS['end'],
        '--window', '0', '0', '2', '2', '--bands', 'Ratio'], env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    row = CubeResolver(root/'c').catalog.iloc[0]
    result = subprocess.run(common+['materialize', '--catalog-root', str(root/'c'), '--item-id', row.item_id,
        '--output', str(root/'m')], env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    r = CubeResolver(root/'m')
    with r.open(r.catalog.iloc[0]) as ds:
        assert set(ds.data_vars) == {'Ratio', 'spatial_ref'}


def test_changed_definition_cannot_masquerade_as_catalog_item(tmp_path):
    sample(tmp_path/'p')
    row = register_view(tmp_path/'c', define_view(tmp_path/'p', 'parent', **OPTIONS))
    changed = define_view(tmp_path/'p', 'parent', bands=['Ratio'], **OPTIONS)
    (tmp_path/'c'/row['view_path']).write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError, match='catalog identity'):
        CubeResolver(tmp_path/'c').open(row)
    with pytest.raises(ValueError, match='catalog identity'):
        materialize_view(tmp_path/'c', row['item_id'], tmp_path/'invalid')
    assert not (tmp_path/'invalid').exists()


def test_repoint_parent_location_keeps_scientific_identity(tmp_path):
    sample(tmp_path/'p')
    definition = define_view(tmp_path/'p', 'parent', **OPTIONS)
    old = register_view(tmp_path/'c', definition)
    shutil.copytree(tmp_path/'p', tmp_path/'relocated')
    moved = define_view(tmp_path/'relocated', 'parent', **OPTIONS)
    assert moved['processing_signature'] == definition['processing_signature']
    new = register_view(tmp_path/'c', moved)
    assert new['item_id'] == old['item_id']
    with CubeResolver(tmp_path/'c').open(new) as ds:
        assert Path(ds.attrs['view_definition']['parent']['zarr_path']).is_relative_to(tmp_path/'relocated')
        assert ds.VV_dB[0, 0, 0].compute().item() == 0


def test_only_selected_mask_window_is_computed(tmp_path):
    group = sample(tmp_path/'p', masked=True)
    group['VV_observed_mask'][0, 23, 29] = 2
    row = register_view(tmp_path/'c', define_view(tmp_path/'p', 'parent',
        start='2026-01-13', end='2026-01-25', window=[1, 1, 2, 2]))
    with CubeResolver(tmp_path/'c').open(row) as ds:
        actual = ds.compute()
        assert actual.VV_observed_mask.shape == (2, 2, 2)
        assert np.isin(actual.VV_observed_mask, [0, 1]).all()
        np.testing.assert_array_equal(actual.x, group['x'][1:3])
        np.testing.assert_array_equal(actual.y, group['y'][1:3])
        np.testing.assert_array_equal(actual.time, group['time'][1:3])
        assert actual.attrs['transform'] == [30, 0, 600030, 0, -30, 9899970]
        assert actual.VV_dB[1, 1, 1] == group['VV_dB'][2, 2, 2]
