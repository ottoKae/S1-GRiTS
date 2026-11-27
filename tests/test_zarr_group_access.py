"""Select child products without borrowing values or metadata from their root."""
import json
import shutil

import numpy as np
import pandas as pd
import pytest
import zarr

from s1grits.analysis import derive_cube
from s1grits.analysis.segmentation import _read_point
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.catalog_validator import validate_catalog_integrity
from s1grits.resolver import CubeResolver
from test_cube_derivatives import source, hashes


@pytest.fixture()
def grouped(tmp_path):
    original = tmp_path/'original'
    source(original)
    record = json.loads(pd.read_parquet(original/'catalog.parquet').to_json(orient='records', date_format='iso'))[0]
    old_path = CubeResolver(original)._asset_path(record)
    root = tmp_path/'catalog'
    bundle = root/'bundle.zarr'
    g = zarr.open_group(str(bundle), mode='w')
    g.attrs.update(crs='EPSG:4326', transform=[1, 0, 0, 0, -1, 0], generation_id='wrong-root')
    g.create_group('products')
    records = []
    for name in ('a', 'b'):
        shutil.copytree(old_path, bundle/'products'/name)
        child = zarr.open_group(str(bundle/'products'/name), mode='r+')
        item = normalize_catalog_record(dict(record, item_id=name, zarr_path='bundle.zarr',
                                             zarr_group=f'products/{name}', processing_signature=f'p_{name}'))
        child.attrs.update(grid_id=item['grid_id'], processing_signature=f'p_{name}', generation_id=f'gen-{name}')
        if name == 'b':
            child['VV_dB'][:] = 9
        records.append(item)
    pd.DataFrame(records).to_parquet(root/'catalog.parquet', index=False)
    return root, records, bundle


def test_normalize_and_read_selected_group(grouped):
    root, records, _ = grouped
    before = hashes(root)
    r = CubeResolver(root)
    for row, value in zip(records, [0, 9]):
        assert row['zarr_group'] == f'products/{row["item_id"]}'
        with r.open(row) as ds:
            assert ds.attrs['crs'] == 'EPSG:32717'
            assert ds.attrs['generation_id'] == f'gen-{row["item_id"]}'
            assert ds.VV_dB[0, 0, 0].item() == value
        assert r.open_group(row).attrs['generation_id'] == ds.attrs['generation_id']
    assert hashes(root) == before


def test_manual_read_uses_selected_group(grouped):
    root, rows, bundle = grouped
    ds = CubeResolver(root)._open_manual(str(bundle), rows[1])
    assert ds.attrs['generation_id'] == 'gen-b'
    np.testing.assert_array_equal(ds.VV_dB, np.full((4, 24, 30), 9))
    manual = CubeResolver(root)._open_manual(str(bundle), rows[0])
    with CubeResolver(root).open(rows[0]) as normal:
        for name in ('VV_dB', 'VH_dB', 'time', 'x', 'y'):
            np.testing.assert_array_equal(manual[name], normal[name])


def test_group_identity_validation_and_missing_group(grouped):
    root, rows, _ = grouped
    result = validate_catalog_integrity(root)
    assert result.valid, result.report()
    duplicate = dict(rows[0], processing_signature='p_other')
    pd.DataFrame(rows+[duplicate]).to_parquet(root/'catalog.parquet', index=False)
    assert any('processing_signatures' in e for e in validate_catalog_integrity(root).errors)
    rows[1]['zarr_group'] = 'products/missing'
    pd.DataFrame(rows).to_parquet(root/'catalog.parquet', index=False)
    assert not validate_catalog_integrity(root).valid
    with pytest.raises(zarr.errors.GroupNotFoundError):
        CubeResolver(root).open(rows[1])


@pytest.mark.parametrize('path', [None, ''])
def test_validation_without_zarr_locator(grouped, path):
    root, rows, _ = grouped
    rows[0]['zarr_path'] = path
    pd.DataFrame(rows).to_parquet(root/'catalog.parquet', index=False)
    result = validate_catalog_integrity(root)
    assert result.valid, result.report()


def test_derived_and_analysis_metadata_come_from_child(grouped, tmp_path):
    root, rows, _ = grouped
    before = hashes(root)
    record = derive_cube(root, 'b', tmp_path/'derived', start='2026-01-01', end='2026-03-01',
                         features=[], window=[0, 0, 2, 2])
    with CubeResolver(tmp_path/'derived').open(record) as ds:
        assert ds.attrs['parent_generation'] == 'gen-b'
        assert ds.attrs['crs'] == 'EPSG:32717'
        assert ds.attrs['parent_reference_item']['zarr_group'] == rows[1]['zarr_group']
        np.testing.assert_array_equal(ds.VV_dB, np.full((4, 2, 2), 9))
    frame, _, lineage, _ = _read_point(root, 'b', tmp_path/'analysis', 'VV_dB', [0, 0],
                                       '2026-01-01', '2026-03-01')
    assert lineage['parent_generation'] == 'gen-b'
    assert lineage['crs'] == 'EPSG:32717'
    np.testing.assert_array_equal(frame.value_db, [9]*4)
    assert hashes(root) == before


def test_cusum_metadata_comes_from_child(grouped, tmp_path):
    pytest.importorskip('nrt')
    from s1grits.analysis import analyze_cusum
    root, _, _ = grouped
    before = hashes(root)
    result = analyze_cusum(root, 'b', tmp_path/'cusum', band='VV_dB',
        history_start='2026-01-01', history_end='2026-01-20', monitor_start='2026-01-21',
        monitor_end='2026-03-01', trend=False, harmonic_order=0, chunk_size=32)
    assert result['status'] == 'complete'
    output = zarr.open_group(str(tmp_path/'cusum/cusum.zarr'), mode='r')
    assert output.attrs['crs'] == 'EPSG:32717'
    assert hashes(root) == before
