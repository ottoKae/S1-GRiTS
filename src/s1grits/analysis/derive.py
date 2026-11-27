"""Read-only Cube derivatives using the existing spatial and monthly operators."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from rasterio.transform import Affine

from s1grits.asf_array_processing import compute_glcm_texture_bands, despeckle_2d, _get_texture_band_names
from s1grits.analysis.cube_ops import clean_backscatter, dual_pol_features, mask_values, parent_masks, select_parent
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.product_instance import make_processing_signature, make_product_variant
from s1grits.resolver import CubeResolver
from s1grits.scenes.blocks import (
    GLCM_BLOCK_HALO, _begin_zarr_timestep_blockwise, _finalize_zarr_timestep_blockwise,
    _iter_spatial_blocks, _linear_to_db, _rollback_zarr_timestep_blockwise, _write_smonthly_block_bands,
)
from s1grits.scenes.smonthly_writer import _monthly_composite_block
from s1grits.scenes.store import _init_zarr_2band
from s1grits.zarr_encoding import create_zarr_array


def derive_cube(catalog_root, item_id, output_root, *, start, end, features=('ratio', 'rvi'),
                spatial_filter='none', monthly=None, window=None, chunk_size=256,
                interpolation=None, interpolation_domain=None, max_gap_days=None) -> dict:
    """Derive one referenced Zarr store over an explicit inclusive UTC interval.

    The catalog item selects the store/instance, not just its catalog timestamp.
    Window is (x offset, y offset, width, height) in the parent's native grid.
    Output is a new Cube root; a nested root is also registered in its source collection.
    """
    root, destination = Path(catalog_root).resolve(), Path(output_root).resolve()
    if root.is_relative_to(destination):
        raise ValueError('Derived output must be separate from the source Cube root')
    if destination.exists():
        raise FileExistsError('Use a new derived output root; existing products are never overwritten')
    if spatial_filter not in ('none', 'tv_bregman', 'nlm') or monthly not in (None, 'mean', 'median'):
        raise ValueError('Supported filters: none/tv_bregman/nlm; monthly methods: mean/median')
    if not isinstance(features, (list, tuple)) or set(features) - {'ratio', 'rvi', 'glcm'}:
        raise ValueError('features must be a list containing ratio, rvi and/or glcm')
    features = [name for name in ('ratio', 'rvi', 'glcm') if name in features]
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError('chunk_size must be a positive integer')
    temporal = None
    if interpolation is not None:
        if interpolation not in ('none', 'linear') or interpolation_domain not in ('db', 'linear_power'):
            raise ValueError('Interpolation requires method none/linear and explicit domain db/linear_power')
        if features or spatial_filter != 'none' or monthly:
            raise ValueError('Run interpolation separately: features=[], filter=none, no monthly composite')
        if interpolation == 'linear':
            if max_gap_days is None or not np.isfinite(max_gap_days) or max_gap_days <= 0:
                raise ValueError('Linear interpolation requires finite positive max_gap_days')
            max_gap_days = float(max_gap_days)
        elif max_gap_days is not None:
            raise ValueError('max_gap_days applies only to linear interpolation')
        temporal = dict(method=interpolation, domain=interpolation_domain, max_gap_days=max_gap_days,
            time_axis='selected_parent_timestamps', extrapolation='none',
            mode='retrospective' if interpolation == 'linear' else 'identity',
            anchor_selection='nearest_valid_before_and_after_per_band_pixel' if interpolation == 'linear' else None,
            masks='per_band_valid_parent_values_and_newly_filled_values')
    elif interpolation_domain is not None or max_gap_days is not None:
        raise ValueError('Select an interpolation method before setting its domain or maximum gap')
    start, end = pd.to_datetime(start, utc=True), pd.to_datetime(end, utc=True)
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError('Provide a valid inclusive UTC start/end interval')
    resolver = CubeResolver(root)
    matches = resolver.catalog[resolver.catalog.item_id == item_id]
    if len(matches) != 1:
        raise ValueError('Select one complete, unambiguous catalog item')
    parent = json.loads(matches.to_json(orient='records', date_format='iso', date_unit='ns'))[0]
    if parent['product_type'] not in ('scenes', 'smonthly', 'monthly'):
        raise ValueError('Derivatives require a time-varying VV_dB/VH_dB Cube')
    if monthly and (parent['product_type'] != 'scenes' or parent.get('track') is None):
        raise ValueError('Monthly derivation requires a scenes Cube with a declared orbit track')
    if not parent.get('processing_signature') or not parent.get('grid_id'):
        raise ValueError('Parent processing signature and grid identity are required')
    path = resolver._asset_path(parent).resolve()
    if destination.is_relative_to(path) or path.is_relative_to(destination):
        raise ValueError('Derived output must be separate from the referenced source store')
    if any((ancestor/'zarr.json').exists() or (ancestor/'.zgroup').exists()
           for ancestor in destination.parents):
        raise ValueError('Derived output must be separate from existing Zarr stores')
    group = resolver.open_group(parent)
    if temporal and any(f'{prefix}_{kind}_mask' in group for prefix in ('VV', 'VH')
                        for kind in ('observed', 'interpolated')):
        raise ValueError('Select the pre-interpolation parent to recompute gaps; filled values are not new observations')
    with resolver.open(parent, chunks={'time': 1, 'y': chunk_size, 'x': chunk_size}) as dataset:
        record = _derive_store(dataset, group, parent, path, destination, start, end,
                               features, spatial_filter, monthly, window, chunk_size, temporal)
    if destination.is_relative_to(root):
        from s1grits.catalog_registration import register_product
        register_product(root, record, source_root=destination)
    return record


def _derive_store(dataset, group, parent, path, destination, start, end,
                  features, spatial_filter, monthly, window, chunk, temporal):
    times, indices, window, native = select_parent(dataset, group.attrs, start, end, window)
    masks = parent_masks(dataset)
    if masks and (spatial_filter != 'none' or monthly or 'glcm' in features):
        raise ValueError('Parent mask propagation is defined only for copy/crop and Ratio/RVI; '
                         'use the pre-interpolation parent for filtering, GLCM or monthly compositing')
    x0, y0, width, height = window
    crs = group.attrs['crs']
    x, y = dataset.x.values, dataset.y.values
    halo = GLCM_BLOCK_HALO if 'glcm' in features else 0
    support = [max(0, x0-halo), max(0, y0-halo), min(len(x), x0+width+halo), min(len(y), y0+height+halo)]
    data = [dataset[b].transpose('time', 'y', 'x') for b in ('VV_dB', 'VH_dB')]
    # Zarr v3 storage fill (legacy stores use 0) is not a scientific NoData declaration.
    fill_values = [array.encoding.get('_FillValue', array.attrs.get('_FillValue')) for array in data]

    def read_db(selected, ys, xs):
        values = []
        for array, fill in zip(data, fill_values):
            values.append(clean_backscatter(array.isel(time=selected, y=ys, x=xs).values, fill))
        return values

    def read_masks(selected, ys, xs):
        return {name: mask.isel(time=selected, y=ys, x=xs).transpose('time', 'y', 'x').values
                for name, mask in masks.items()}

    # Fixed strips make the decoded-input fingerprint independent of output chunks.
    digest = hashlib.sha256(json.dumps({'times_ns': times[indices].asi8.tolist(),
        'support': support, 'crs': crs, 'transform': list(native)[:6]}, sort_keys=True).encode())
    for index in indices:
        for row in range(support[1], support[3], 128):
            for array in read_db([index], slice(row, min(row+128, support[3])), slice(support[0], support[2])):
                digest.update(array.astype('<f4', copy=False).tobytes())
            for array in read_masks([index], slice(row, min(row+128, support[3])), slice(support[0], support[2])).values():
                digest.update(mask_values(array).tobytes())
    texture = dict(enabled=True, inputs=['VV_dB', 'VH_dB'],
                   metrics=['contrast', 'homogeneity', 'entropy', 'correlation'],
                   window_size=5, distance=1, angles=[0, 90], average_angles=True, levels=16,
                   vv_db_range=[-25, 5], vh_db_range=[-32, -5])
    contract = {
        'contract_version': 'cube_derivative_v1', 'parent_processing_signature': parent['processing_signature'],
        'parent_item_id': parent['item_id'], 'parent_track': parent.get('track'),
        'parent_generation': group.attrs.get('generation_id'),
        'parent_grid_id': parent['grid_id'], 'parent_values_sha256': digest.hexdigest(),
        'start_utc': start.isoformat(), 'end_utc': end.isoformat(), 'window': list(window), 'support': support,
        'spatial_filter': spatial_filter, 'filter_domain': 'dB_internal_via_linear_power' if spatial_filter != 'none' else None,
        'filter_preset': {'tv_bregman': 'reg5', 'nlm': 'patch3_distance7_h_auto'}.get(spatial_filter),
        'monthly': monthly, 'monthly_domain': 'linear_power_from_parent_dB' if monthly else None,
        'monthly_validity': 'paired_finite_positive' if monthly else None,
        'features': features, 'glcm_preset': texture if 'glcm' in features else None,
        'operator_order': ['parent_dB', 'monthly_linear' if monthly else 'native_times',
                           spatial_filter, 'features', 'crop'],
        'filter_support': 'entire_selected_support_per_output_time',
    }
    if temporal:
        contract['temporal_interpolation'] = temporal
        contract['operator_order'] = ['parent_dB', 'temporal_interpolation', 'crop']
        contract['filter_support'] = None
    if masks:
        contract.update(contract_version='cube_derivative_v2', parent_mask_names=list(masks),
                        mask_propagation='preserve_per_band_at_selected_times_and_pixels')
    signature = make_processing_signature(contract)
    product = 'smonthly' if monthly else parent['product_type']
    label = f'{product}_{parent["flight_direction"]}_derived_{signature}'
    tile_dir = destination/parent['tile_id']
    store_path = tile_dir/label/'zarr'/f'derived_{signature}.zarr'
    bands = ['VV_dB', 'VH_dB'] + [n for n in ('Ratio', 'RVI') if n.lower() in features]
    if 'glcm' in features:
        bands += _get_texture_band_names(texture)
    transform = native*Affine.translation(x0, y0)
    store_path.parent.mkdir(parents=True)
    output = _init_zarr_2band(store_path, x[x0:x0+width], y[y0:y0+height], crs,
                              transform, chunk, chunk, 'derived', band_names=bands)
    if monthly:
        count = create_zarr_array(output, 'n_obs', shape=(0, height, width), chunks=(1, chunk, chunk),
                                   dtype='uint32', fill_value=0, dimension_names=['time', 'y', 'x'])
        count.attrs.update({'_ARRAY_DIMENSIONS': ['time', 'y', 'x'], 'grid_mapping': 'spatial_ref',
                            'description': 'Paired valid parent scene observations in this selected month; not burst count.'})
        bands.append('n_obs')
    if temporal:
        for prefix in ('VV', 'VH'):
            for kind in ('observed', 'interpolated'):
                name = f'{prefix}_{kind}_mask'
                mask = create_zarr_array(output, name, shape=(0, height, width), chunks=(1, chunk, chunk),
                    dtype='uint8', fill_value=0, dimension_names=['time', 'y', 'x'])
                mask.attrs.update({'_ARRAY_DIMENSIONS': ['time', 'y', 'x'], 'grid_mapping': 'spatial_ref',
                    'description': '1 = valid value in selected parent; not a raw observation count' if kind == 'observed'
                                   else '1 = missing parent value filled by this interpolation run'})
                bands.append(name)
    for name in masks:
        mask = create_zarr_array(output, name, shape=(0, height, width), chunks=(1, chunk, chunk),
            dtype='uint8', fill_value=0, dimension_names=['time', 'y', 'x'])
        mask.attrs.update(dict(masks[name].attrs, _ARRAY_DIMENSIONS=['time', 'y', 'x']))
        bands.append(name)
    variant = 'derived_' + make_product_variant(product, {
        'processing.spatial_despeckle': spatial_filter != 'none',
        'processing.despeckle_method': spatial_filter,
        'processing.despeckle_strength': 5 if spatial_filter == 'tv_bregman' else None,
        'processing.composite_method': monthly,
    }, bands)
    if temporal:
        variant += f'_interp_{temporal["method"]}_{temporal["domain"]}'
    output.attrs.update({'status': 'building', 'product_type': product, 'product_label': label,
        'processing_signature': signature, 'processing_variant_json': json.dumps(contract), 'product_variant': variant,
        'geometry_group_id': parent.get('geometry_group_id'), 'parent_reference_item': parent,
        'parent_zarr': str(path.resolve()), 'parent_snapshot_sha256': digest.hexdigest(),
        'parent_generation': group.attrs.get('generation_id'),
        'parent_snapshot_scope': 'Decoded selected VV/VH, support grid and timestamps'
                                 + (', including per-band masks' if masks else '')
                                 + '; source must be quiescent.'})
    if monthly:
        periods = pd.period_range(start.tz_localize(None), end.tz_localize(None), freq='M')
        groups = [(p.start_time, [i for i in indices if times[i].strftime('%Y-%m') == str(p)]) for p in periods]
    else:
        groups = [(times[i].tz_localize(None), [i]) for i in indices]
    if temporal:
        from .interpolation import _write_interpolated_store
        _write_interpolated_store(output, read_db, times, indices, window, chunk, temporal)
    for stamp, selected in (groups if temporal is None else ()):
        def read_composite(ys, xs):
            if not selected:
                shape = (ys.stop-ys.start, xs.stop-xs.start)
                return np.full(shape, np.nan, 'f4'), np.full(shape, np.nan, 'f4'), np.zeros(shape, 'u4')
            vv, vh = read_db(selected, ys, xs)
            if not monthly:
                return vv[0], vh[0], None
            with np.errstate(over='ignore', invalid='ignore'):
                vv, vh = np.power(np.float32(10), vv/10), np.power(np.float32(10), vh/10)
            valid = np.isfinite(vv) & np.isfinite(vh) & (vv > 0) & (vh > 0)
            vv[~valid], vh[~valid] = np.nan, np.nan
            return (_linear_to_db(_monthly_composite_block(list(vv), monthly, 0)),
                    _linear_to_db(_monthly_composite_block(list(vh), monthly, 0)), valid.sum(axis=0, dtype='u4'))

        filtered = None
        if spatial_filter != 'none':
            vv, vh, counts = read_composite(slice(support[1], support[3]), slice(support[0], support[2]))
            filtered = []
            for value in (vv, vh):
                if np.isfinite(value).any():
                    power = np.power(np.float32(10), value/10)
                    filtered.append(_linear_to_db(despeckle_2d(power, method=spatial_filter)))
                else:
                    filtered.append(value)
        time_index, key = _begin_zarr_timestep_blockwise(output, stamp.to_datetime64(), bands)
        try:
            for ys, xs in _iter_spatial_blocks(height, width, chunk, chunk):
                ey0, ey1 = max(support[1], y0+ys.start-halo), min(support[3], y0+ys.stop+halo)
                ex0, ex1 = max(support[0], x0+xs.start-halo), min(support[2], x0+xs.stop+halo)
                if filtered is None:
                    a, b, n = read_composite(slice(ey0, ey1), slice(ex0, ex1))
                else:
                    fy, fx = slice(ey0-support[1], ey1-support[1]), slice(ex0-support[0], ex1-support[0])
                    a, b = [v[fy, fx] for v in filtered]
                    n = counts[fy, fx] if monthly else None
                values = {'VV_dB': a, 'VH_dB': b}
                values.update(dual_pol_features(a, b, features))
                if 'glcm' in features:
                    arrays, names = compute_glcm_texture_bands(a, b, texture)
                    values.update(zip(names, arrays))
                if monthly:
                    values['n_obs'] = n
                if masks:
                    values.update({name: value[0] for name, value in
                                   read_masks(selected, slice(ey0, ey1), slice(ex0, ex1)).items()})
                crop = (slice(y0+ys.start-ey0, y0+ys.stop-ey0), slice(x0+xs.start-ex0, x0+xs.stop-ex0))
                _write_smonthly_block_bands(output, time_index, ys, xs, bands,
                                             {name: value[crop] for name, value in values.items()})
            _finalize_zarr_timestep_blockwise(output, time_index, key)
        except Exception:
            _rollback_zarr_timestep_blockwise(output, time_index, bands)
            raise
    record = normalize_catalog_record(dict(
        item_id=f'{parent["tile_id"]}_{label}', tile_id=parent['tile_id'], product_type=product,
        output_type=product, product_label=label, flight_direction=parent['flight_direction'], track=parent.get('track'),
        geometry_group_id=parent.get('geometry_group_id'), crs=crs, transform=list(transform)[:6], width=width, height=height,
        bands=bands, processing_signature=signature, processing_variant_json=json.dumps(contract), product_variant=variant,
        processing_level='derived', datetime=groups[0][0], start_datetime=groups[0][0], end_datetime=groups[-1][0],
        month=groups[0][0].strftime('%Y-%m'), zarr_path=store_path.relative_to(tile_dir).as_posix()))
    serialized_record = json.loads(pd.DataFrame([record]).to_json(orient='records', date_format='iso', date_unit='ns'))[0]
    output.attrs.update({'derived_record': serialized_record, 'status': 'complete'})
    pd.DataFrame([record]).to_parquet(destination/'catalog.parquet')
    pd.DataFrame([record]).to_parquet(tile_dir/'catalog.parquet')
    (destination/'derivation.json').write_text(json.dumps({'status': 'complete', 'contract': contract,
        'parent_reference_item': parent, 'parent_store': str(path.resolve()), 'record': serialized_record,
        'input_times_utc': times[indices].astype(str).tolist(),
        'output_time_sources': None if temporal else
                               {str(t): times[ids].astype(str).tolist() for t, ids in groups}}, indent=2), encoding='utf-8')
    return record
