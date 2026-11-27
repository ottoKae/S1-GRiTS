"""Web plans drive real tiny COG workflows; only the network boundary is replaced."""
from copy import deepcopy
import sys

import numpy as np
import pytest
import yaml

pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient
from s1grits.webapp.server import create_app
from s1grits.resolver import CubeResolver
from s1grits.config_schema import find_unknown_config_keys
from test_provenance_workflow import Case, CRS, TRANSFORM, _seed
from test_nlooks_weighted import rtc_id, weight_file


@pytest.mark.parametrize('options', [
    {}, {'spatial_filter':'tv_bregman'}, {'spatial_filter':'nlm'},
    {'features_ratio':True}, {'features_rvi':True}, {'features_glcm':True},
    {'workflow':'monthly','monthly_method':'mean'},
    {'workflow':'monthly','monthly_method':'median'},
    {'intra_pass_method':'average'}, {'intra_pass_method':'bursts_center'},
    {'intra_pass_method':'nlooks_weighted'},
])
def test_production(tmp_path_factory, monkeypatch, options):
    tmp_path = tmp_path_factory.mktemp('p')
    monkeypatch.chdir(tmp_path)
    case = Case(tmp_path, monkeypatch)
    if options.get('intra_pass_method') == 'nlooks_weighted':
        for row in case.rows:
            row['jpl_burst_id'] = f"T018-03865{row['pass_id']-18 + (row['jpl_burst_id']=='B002')}-IW2"
            row['opera_id'] = rtc_id(row['jpl_burst_id']).replace('20260107', f"2025{row['acq_dt'].month:02d}05")
        for burst in {row['jpl_burst_id'] for row in case.rows}:
            weight_file(tmp_path/'looks', burst, dict(crs=CRS, transform=TRANSFORM, width=4, height=3,
                        nodata=np.nan), np.full((3,4), 2, dtype='f4'))
        options = {**options, 'looks_directory':'looks'}
    client = TestClient(create_app(tmp_path))
    response = client.post('/api/plan', json=dict(tiles=['17MPU'], years=[2025], months=[1,2],
        output_subdir='w', max_workers=1, **options))
    assert response.status_code == 200, response.text
    plan = response.json()
    config = client.app.state.console._config(plan, 'ASCENDING')
    assert find_unknown_config_keys(config) == []
    # Keep the Web's science choices; bound the grid and concurrency to the real COG fixture.
    config['processing'].update(tile_clip=False, zarr_chunks={'y':2,'x':2})
    config['roi']['min_tile_coverage_frac'] = 0
    config['memory'] = case.config['memory']
    config['query'] = case.config['query']
    config['preflight'] = case.config['preflight']
    config['runtime'] = case.config['runtime']
    actual_root = tmp_path/plan.get('result_subdir', plan['output_subdir'])
    _seed(actual_root, track_token="99")
    result = case.run(config)
    assert result['17MPU']['status'] == 'success', result
    actual = CubeResolver(actual_root)
    assert len(actual.catalog) > 0
    # A direct CLI run of the generated YAML must produce the same identities and samples.
    reference_config = deepcopy(config)
    reference_config['output']['base_dir'] = str(tmp_path/'r')
    method = options.get('intra_pass_method','first_valid_pixel')
    reference_root = tmp_path/'r'
    if method != 'first_valid_pixel':
        reference_root = reference_root/'derived'/f'intra_pass_{method}'
    _seed(reference_root, track_token="99")
    config_path = tmp_path/'reference.yaml'
    config_path.write_text(yaml.safe_dump(reference_config), encoding='utf-8')
    from s1grits import cli
    monkeypatch.setattr(sys, 'argv', ['s1grits','process_scenes','--config',str(config_path)])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    reference = CubeResolver(reference_root)
    assert len(actual.catalog) == len(reference.catalog)
    for _, record in actual.catalog.iterrows():
        if '2024' in str(record.datetime):
            continue
        expected = reference.catalog.loc[reference.catalog.item_id == record.item_id].iloc[0]
        assert record.processing_signature == expected.processing_signature
        with actual.open(record) as ds, reference.open(expected) as other:
            assert set(ds.data_vars) == set(other.data_vars)
            for name in ds.data_vars:
                np.testing.assert_array_equal(ds[name].values, other[name].values)
            if options.get('features_ratio'):
                assert 'Ratio' in ds
            if options.get('features_rvi'):
                assert 'RVI' in ds
            if options.get('features_glcm'):
                assert len([name for name in ds.data_vars if '_glcm_' in name]) == 8


@pytest.mark.parametrize('options', [
    {'intra_pass_method':'average','workflow':'monthly'},
    {'intra_pass_method':'bursts_center','spatial_filter':'tv_bregman'},
    {'intra_pass_method':'average','include_static':True},
    {'intra_pass_method':'average','target_resolution':10},
    {'intra_pass_method':'nlooks_weighted'}, {'spatial_filter':'unknown'},
    {'monthly_method':'unknown'},
])
def test_unsupported_production_combinations_fail_preflight(tmp_path, options):
    client = TestClient(create_app(tmp_path))
    response = client.post('/api/plan', json=dict(tiles=['17MPU'], years=[2025], months=[1], **options))
    assert response.status_code == 400, response.text
    assert not (tmp_path/'s1_cube').exists()
