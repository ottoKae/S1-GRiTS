"""Small on-disk products exercise selection without hiding assets or variables."""
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.resolver import CubeResolver
from test_cube_derivatives import hashes


TILE = '17MPU'


def product(root, name, *, kind='scenes', signature='p1', value=1,
            direction='ASCENDING', offset=0, band='VV_dB'):
    coords = {'x': offset + np.array([600015., 600045.]),
              'y': np.array([9899985., 9899955.])}
    dims = ('y', 'x')
    shape = (2, 2)
    if kind != 'static':
        coords['time'] = np.array(['2026-01-01', '2026-02-01'], dtype='datetime64[ns]')
        dims = ('time', 'y', 'x')
        shape = (2, 2, 2)
    ds = xr.Dataset({band: (dims, np.full(shape, value, dtype='f4'))}, coords=coords)
    path = root / TILE / f'{name}.zarr'
    ds.to_zarr(path, mode='w', consolidated=False)
    return normalize_catalog_record(dict(
        item_id=name, tile_id=TILE, product_type=kind, processing_signature=signature,
        flight_direction=direction, track=18, geometry_group_id=f'{TILE}_{direction}_TK18',
        crs='EPSG:32717', transform=[30, 0, 600000 + offset, 0, -30, 9900000],
        width=2, height=2, datetime='2026-01-01', zarr_path=f'{name}.zarr', bands=[band],
    ))


def resolver(root, records):
    pd.DataFrame(records).to_parquet(root / 'catalog.parquet', index=False)
    return CubeResolver(root)


@pytest.mark.parametrize('second_kind', ['scenes', 'smonthly'])
def test_conflicting_dynamic_variables_remain_separate(tmp_path, second_kind):
    a = product(tmp_path, 'a')
    b = product(tmp_path, 'b', kind=second_kind, signature='p2', value=9)
    r = resolver(tmp_path, [a, b])
    before = hashes(tmp_path)
    if second_kind == 'scenes':
        with pytest.raises(ValueError, match='distinct processing_signatures'):
            r.open_stack(TILE)
    result = r.open_stack(TILE, require_same_processing=second_kind != 'scenes')
    assert isinstance(result, dict)
    keys = ['scenes:p1', 'scenes:p2'] if second_kind == 'scenes' else ['scenes', second_kind]
    assert set(result) == set(keys)
    for key, value in zip(keys, [1, 9]):
        np.testing.assert_array_equal(result[key]['VV_dB'].values, np.full((2, 2, 2), value))
    assert hashes(tmp_path) == before


@pytest.mark.parametrize('with_dynamic', [False, True])
def test_static_variants_are_not_overwritten(tmp_path, with_dynamic):
    records = [product(tmp_path, 'a', kind='static', band='angle'),
               product(tmp_path, 'b', kind='static', band='angle', signature='p2', value=9)]
    if with_dynamic:
        records.append(product(tmp_path, 'dynamic'))
    r = resolver(tmp_path, records)
    result = r.open_stack(TILE, require_same_processing=False)
    assert isinstance(result, dict)
    assert set(result) == {'static:p1', 'static:p2'} | ({'scenes'} if with_dynamic else set())
    for key, value in [('static:p1', 1), ('static:p2', 9)]:
        np.testing.assert_array_equal(result[key]['angle'].values, np.full((2, 2), value))


def test_static_and_dynamic_same_band_stay_separate(tmp_path):
    r = resolver(tmp_path, [product(tmp_path, 'dynamic'),
                            product(tmp_path, 'static', kind='static', value=9)])
    result = r.open_stack(TILE)
    assert isinstance(result, dict)
    assert result['scenes']['VV_dB'].dims == ('time', 'y', 'x')
    np.testing.assert_array_equal(result['static']['VV_dB'].values, np.full((2, 2), 9))


@pytest.mark.parametrize('entry', ['stack', 'training_dynamic', 'training_static'])
def test_multiple_assets_rejected_before_read_or_write(tmp_path, entry):
    kind = 'static' if entry == 'training_static' else 'scenes'
    records = [product(tmp_path, 'a', kind=kind), product(tmp_path, 'b', kind=kind, value=9)]
    if entry != 'stack':
        records.append(product(tmp_path, 'other', kind='scenes' if kind == 'static' else 'static',
                               band='other'))
    r = resolver(tmp_path, records)
    before = hashes(tmp_path)
    with pytest.raises(ValueError, match='Multiple Zarr assets'):
        if entry == 'stack':
            r.open_stack(TILE)
        else:
            r.materialize_training_cube(TILE, tmp_path / 'training.zarr')
    assert hashes(tmp_path) == before
    assert not (tmp_path / 'training.zarr').exists()
    # The existing low-level API remains available for an explicit selection.
    selected = r.query(tile_id=TILE, product_type=kind)
    ds = r.open(selected[selected.item_id == 'b'].iloc[0])
    assert np.all(ds['VV_dB'].values == 9)


def test_same_store_multiple_rows_and_path_spellings_open_once(tmp_path):
    a = product(tmp_path, 'a')
    b = dict(a, item_id='a-feb', datetime='2026-02-01',
             zarr_path=str(tmp_path / TILE / 'a.zarr'), zarr_group='')
    r = resolver(tmp_path, [a, b])
    result = r.open_stack(TILE)
    assert isinstance(result, xr.Dataset)
    assert result.sizes['time'] == 2
    np.testing.assert_array_equal(result['VV_dB'].values, np.ones((2, 2, 2)))


def test_distinct_groups_are_distinct_assets(tmp_path):
    a = product(tmp_path, 'a')
    r = resolver(tmp_path, [dict(a, zarr_group='one'), dict(a, zarr_group='two')])
    with pytest.raises(ValueError, match='Multiple Zarr assets'):
        r.open_stack(TILE)


@pytest.mark.parametrize('entry', ['aligned', 'stack'])
def test_direction_selection_precedes_grid_and_signature_checks(tmp_path, entry):
    asc = product(tmp_path, 'asc')
    desc = product(tmp_path, 'desc', direction='DESCENDING', offset=30, signature='p2', value=9)
    r = resolver(tmp_path, [asc, dict(asc, datetime='2026-02-01'), desc])
    before = hashes(tmp_path)
    if entry == 'aligned':
        selected = r.get_aligned_products(TILE, direction='DESCENDING', require_same_grid=True)
        assert list(selected['scenes']['item_id']) == ['desc']
    else:
        np.testing.assert_array_equal(r.open_stack(TILE, direction='DESCENDING')['VV_dB'],
                                      np.full((2, 2, 2), 9))
    # Unspecified direction keeps the established majority-grid rule.
    np.testing.assert_array_equal(r.open_stack(TILE)['VV_dB'], np.ones((2, 2, 2)))
    assert hashes(tmp_path) == before


@pytest.mark.parametrize('same_time', [False, True])
def test_distinct_variables_keep_existing_time_merge_rule(tmp_path, same_time):
    a = product(tmp_path, 'a')
    b = product(tmp_path, 'b', kind='smonthly', band='VH_dB', value=9)
    if not same_time:
        import zarr
        group = zarr.open_group(str(tmp_path / TILE / 'b.zarr'), mode='r+')
        group['time'][:] += 1
    r = resolver(tmp_path, [a, b])
    before = hashes(tmp_path)
    result = r.open_stack(TILE)
    if same_time:
        assert isinstance(result, xr.Dataset)
        np.testing.assert_array_equal(result['VV_dB'], np.ones((2, 2, 2)))
        np.testing.assert_array_equal(result['VH_dB'], np.full((2, 2, 2), 9))
    else:
        assert isinstance(result, dict)
        assert set(result) == {'scenes', 'smonthly'}
        assert not np.array_equal(result['scenes'].time.values, result['smonthly'].time.values)
    assert hashes(tmp_path) == before
