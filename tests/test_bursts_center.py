"""Centre selection uses full sources, paired validity and stable provenance."""
import json

import numpy as np
import pandas as pd
import pytest
import rasterio
import zarr
from rasterio.transform import Affine

from s1grits.scenes.center import burst_centers
from s1grits.scenes.scene_writer import _write_scenes_output
from s1grits.scenes.average import validate_intra_pass_config
from s1grits.analysis.catalog import resync_catalog_from_filesystem
from s1grits.stac_builder import set_stac_output_enabled, stac_output_enabled
from test_intra_pass_average import inputs, fingerprint


def center_inputs(root, chunk):
    options = inputs(root, method='bursts_center', chunk=chunk)
    options.update(width=10, height=6, features_ratio=True, features_rvi=True,
                   x_coords=600015+30*np.arange(10), y_coords=9899985-30*np.arange(6))
    vv = [np.full((6, 10), 1, dtype='f4'), np.full((6, 10), 9, dtype='f4')]
    vv[0][:, 8:] = np.nan
    vv[1][:, :2] = np.nan
    options.update(final_vv=vv, final_vh=[a*0.25 for a in vv],
                   prof_vv=[dict(options['prof_vv'][0],height=6,width=10)]*2,
                   prof_vh=[dict(options['prof_vh'][0],height=6,width=10)]*2)
    return options


@pytest.mark.parametrize('chunk,threads', [(2,1),(3,2),(16,1)])
def test_selection(tmp_path, chunk, threads):
    options = center_inputs(tmp_path/'17MPU', chunk)
    options['blockwise_threads'] = threads
    previous = stac_output_enabled()
    set_stac_output_enabled(False)
    try:
        record, = _write_scenes_output(**options)
    finally:
        set_stac_output_enabled(previous)
    g = zarr.open_group(str(options['tile_dir']/record['zarr_path']),mode='r')
    expected = np.tile([1]*5+[2]*5, (6,1))
    np.testing.assert_array_equal(g['source_index'][0], expected)
    np.testing.assert_allclose(g['VV_dB'][0], np.where(expected==1,0,10*np.log10(9)),atol=1e-6)
    np.testing.assert_allclose(g['VH_dB'][0], np.where(expected==1,10*np.log10(.25),10*np.log10(2.25)),atol=1e-6)
    np.testing.assert_array_equal(g['n_candidates'][0], np.tile([1,1,2,2,2,2,2,2,1,1],(6,1)))
    np.testing.assert_allclose(g['Ratio'][0],.25)
    np.testing.assert_allclose(g['RVI'][0],.8)
    assert 'n_contributors' not in g
    assert g['source_index'].dtype == np.dtype('uint32')
    sources = g.attrs['acquisitions_by_time']['20260107T000000']
    assert sources['opera_ids'] == ['O0','O2']
    assert sources['centers_target_xy'] == [[600120.,9899910.],[600180.,9899910.]]
    contract = json.loads(g.attrs['processing_variant_json'])
    assert contract['processing.intra_pass_mosaic.method'] == 'bursts_center'
    # Cropping the output must not recalculate source centres from that crop.
    options.update(tile_dir=tmp_path/'crop', width=4, x_coords=options['x_coords'][3:7],
                   transform=options['transform']*Affine.translation(3,0))
    crop, = _write_scenes_output(**options)
    cropped=zarr.open_group(str(options['tile_dir']/crop['zarr_path']),mode='r')
    np.testing.assert_array_equal(cropped['source_index'][0], expected[:,3:7])
    assert cropped.attrs['acquisitions_by_time']==g.attrs['acquisitions_by_time']


def test_ties_nodata_and_resync(tmp_path):
    options = center_inputs(tmp_path/'17MPU',3)
    # Equal centres: the lower OPERA ID wins, even after reversing inputs.
    vv = np.arange(1,61,dtype='f4').reshape(6,10)
    vv[0,0] = -9999
    options.update(final_vv=[vv*2,vv],final_vh=[vv*.5,vv*.25],clean_source_rows=[2,0],
                   generate_cog=True)
    # Keep finite nodata identical in the profiles and all polarizations.
    for a in options['final_vv']+options['final_vh']:
        a[0,0]=-9999
    record, = _write_scenes_output(**options)
    g=zarr.open_group(str(options['tile_dir']/record['zarr_path']),mode='r')
    assert g['source_index'][0,0,0]==0 and g['n_candidates'][0,0,0]==0
    np.testing.assert_array_equal(g['source_index'][0,1:],1)
    np.testing.assert_allclose(g['VV_dB'][0,1:],10*np.log10(vv[1:]),atol=1e-6)
    before=fingerprint(options['tile_dir'])
    options['final_vv'][1][0,1]=np.nan
    with pytest.raises(ValueError,match='centres changed'):
        _write_scenes_output(**options)
    assert fingerprint(options['tile_dir'])==before
    previous=stac_output_enabled()
    try:
        result=resync_catalog_from_filesystem(tmp_path,write_stac=True,stac_format='json')
    finally:
        set_stac_output_enabled(previous)
    assert result['success']
    rebuilt=pd.read_parquet(tmp_path/'catalog.parquet').iloc[0]
    assert rebuilt['item_id']==record['item_id']
    assert list(rebuilt['opera_ids'])==['O0','O2']
    item=json.loads((options['tile_dir']/rebuilt['item_path']).read_text())
    assert item['id']==record['item_id']
    assert 'source_index' in item['properties']['cube:dimensions']['spectral']['values']
    with rasterio.open(options['tile_dir']/record['cog_path']) as cog:
        assert set(cog.descriptions)=={'VV_dB','VH_dB','Ratio','RVI'}
    assert set(b['name'] for b in item['assets']['cog']['eo:bands'])==set(cog.descriptions)


def test_centroid_reads_strips_and_rejects_incompatible_pairs():
    options=center_inputs(None,2)

    class StripsOnly:
        def __init__(self,array):
            self.array=array
            self.shape=array.shape
        def __array__(self,*args,**kwargs):
            raise AssertionError('full burst materialised')
        def __getitem__(self,key):
            assert key[0].stop-key[0].start <= 256
            return self.array[key]

    centers=burst_centers([StripsOnly(a) for a in options['final_vv']],options['prof_vv'],
                         [StripsOnly(a) for a in options['final_vh']],options['prof_vh'],'EPSG:32717')
    assert centers==[[600120.,9899910.],[600180.,9899910.]]
    options['prof_vh'][0]=dict(options['prof_vh'][0],transform=Affine(30,0,600015,0,-30,9900000))
    with pytest.raises(ValueError,match='same native'):
        burst_centers(options['final_vv'],options['prof_vv'],options['final_vh'],options['prof_vh'],'EPSG:32717')
    with pytest.raises(ValueError,match='metres'):
        burst_centers([],[],[],[],'EPSG:4326')


def test_center_config():
    config={'processing':{'intra_pass_mosaic':{'method':'bursts_center'}},'roi':{'flight_direction':'ASCENDING'}}
    assert validate_intra_pass_config(config)=='bursts_center'
    config['processing']['monthly']={'enabled':True}
    with pytest.raises(ValueError,match='bursts_center monthly'):
        validate_intra_pass_config(config)
