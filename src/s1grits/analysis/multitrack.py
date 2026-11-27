"""Explicit, same-grid monthly track combinations using local view storage."""
from contextlib import ExitStack
import json
from pathlib import Path
import re
import warnings

import numpy as np
import pandas as pd
import xarray as xr

from s1grits.analysis.cube_ops import clean_backscatter
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.product_instance import make_processing_signature
from s1grits.resolver import CubeResolver

MULTITRACK_VERSION = 's1grits_multitrack_monthly_v1'


def define_multitrack_view(catalog_root, item_ids, *, start_month, end_month, method, window=None):
    """Select ordered entity monthly parents; return metadata without writing pixels.

    First release accepts monthly derive_cube outputs with recorded statistical
    ranges. Legacy production monthly labels alone cannot establish those ranges.
    """
    if not isinstance(item_ids, (list, tuple)) or len(item_ids) < 2 or len(set(item_ids)) != len(item_ids):
        raise ValueError('Select at least two distinct ordered monthly parent items')
    resolver = CubeResolver(catalog_root)
    parents = []
    for item_id in item_ids:
        matches = resolver.catalog[resolver.catalog.item_id == item_id]
        if len(matches) != 1:
            raise ValueError('Select one unambiguous item per parent instance')
        parent = json.loads(matches.to_json(orient='records', date_format='iso', date_unit='ns'))[0]
        if parent.get('view_path'):
            raise ValueError('Monthly combinations require entity parents, not nested views')
        parent['zarr_path'] = str(resolver._asset_path(parent).resolve())
        parents.append(parent)
    contract = describe_multitrack(parents, start_month, end_month, method, window)
    return dict(version=MULTITRACK_VERSION, parents=parents, contract=contract,
                processing_signature=make_processing_signature(contract))


def open_multitrack(definition, *, chunks=None):
    """Lazy month alignment and paired reduction shared by views and materialization."""
    if make_processing_signature(definition['contract']) != definition.get('processing_signature'):
        raise ValueError('Invalid view scientific signature')
    resources = ExitStack()
    try:
        resolver = CubeResolver('.')
        datasets = [resources.enter_context(resolver.open(p, chunks=chunks if chunks is not None else {}))
                    for p in definition['parents']]
        contract = definition['contract']
        actual = describe_multitrack(definition['parents'], contract['start_month'], contract['end_month'],
                                    contract['method'], contract['window'], datasets=datasets)
        if actual != contract:
            raise ValueError('Parent version or metadata changed; define a new view')
        if contract['parent_version_evidence'] == 'metadata_only':
            warnings.warn('Parent has no generation_id: metadata checks cannot detect in-place value changes',
                          UserWarning, stacklevel=2)
        x0, y0, width, height = contract['window']
        target_times = pd.to_datetime(contract['times']).values
        arrays = []
        for dataset in datasets:
            month_times = pd.to_datetime(dataset.time.values).to_period('M').to_timestamp().values
            selected = dataset.assign_coords(time=month_times).isel(y=slice(y0, y0+height), x=slice(x0, x0+width))
            for name in ('VV_dB', 'VH_dB'):
                array = selected[name].transpose('time', 'y', 'x')
                fill = array.encoding.get('_FillValue', array.attrs.get('_FillValue'))
                cleaned = xr.apply_ufunc(clean_backscatter, array, kwargs={'fill': fill},
                                         dask='parallelized', output_dtypes=[np.float32])
                arrays.append(cleaned.reindex(time=target_times))
        outputs = xr.apply_ufunc(
            _combine_block, *arrays, kwargs={'method': contract['method']},
            output_core_dims=[[]]*(3+len(datasets)), dask='parallelized',
            output_dtypes=[np.float32, np.float32, np.uint32]+[np.uint8]*len(datasets))
        values = dict(zip(contract['bands'], outputs))
        for name, array in values.items():
            array.attrs = {'units': 'dB' if name.endswith('_dB') else '1'}
        values['n_contributing_tracks'].attrs['description'] = 'Actual contributing track-month products; not n_obs'
        for parent, name in zip(contract['parents'], contract['bands'][3:]):
            values[name].attrs.update(description='1 if this parent track-month contributes, else 0',
                                      parent_item_id=parent['parent_identity']['item_id'], track=parent['track'])
        record = multitrack_record(definition)
        result = xr.Dataset(values, attrs={
            'item_id': record['item_id'], 'crs': record['crs'], 'transform': record['transform'],
            'grid_id': record['grid_id'], 'processing_signature': record['processing_signature'],
            'representation': 'virtual', 'view_definition': definition,
            'parent_version_evidence': contract['parent_version_evidence'],
            'time_semantics': 'UTC calendar-month labels, not acquisition dates',
        })
        result.set_close(resources.close)
        return result
    except Exception:
        resources.close()
        raise


def _combine_block(*arrays, method):
    shape = arrays[0].shape
    vv, vh = np.full(shape, np.nan, 'f4'), np.full(shape, np.nan, 'f4')
    count = np.zeros(shape, 'u4')
    totals = [np.zeros(shape, 'f8'), np.zeros(shape, 'f8')] if method == 'mean' else None
    contributions = []
    for a, b in zip(arrays[::2], arrays[1::2]):
        with np.errstate(over='ignore', invalid='ignore'):
            av, bv = np.power(10., a.astype('f8')/10), np.power(10., b.astype('f8')/10)
        valid = np.isfinite(av) & np.isfinite(bv) & (av > 0) & (bv > 0)
        take = valid & (count == 0) if method == 'first_valid_pixel' else valid
        if method == 'first_valid_pixel':
            vv[take], vh[take] = a[take], b[take]
        else:
            totals[0][take] += av[take]
            totals[1][take] += bv[take]
        count[take] += 1
        contributions.append(take.astype('u1'))
    if method == 'mean':
        valid = count > 0
        vv[valid] = 10*np.log10(totals[0][valid]/count[valid])
        vh[valid] = 10*np.log10(totals[1][valid]/count[valid])
    return (vv, vh, count, *contributions)


def describe_multitrack(parents, start_month, end_month, method, window, datasets=None):
    """Validate external parent metadata once and freeze the finite input contract."""
    from s1grits.analysis.views import _describe

    if method not in ('first_valid_pixel', 'mean'):
        raise ValueError('Explicit method must be first_valid_pixel or mean')
    if any(not isinstance(m, str) or not re.fullmatch(r'\d{4}-\d{2}', m) for m in (start_month, end_month)):
        raise ValueError('Use explicit YYYY-MM start_month and end_month')
    periods = pd.period_range(start_month, end_month, freq='M')
    if not len(periods):
        raise ValueError('start_month must not follow end_month')
    resolver, descriptions, assets, stages, ranges = CubeResolver('.'), [], set(), [], []
    with ExitStack() as resources:
        for i, parent in enumerate(parents):
            track = parent.get('track')
            if (parent.get('view_path') or parent.get('product_type') not in ('smonthly', 'monthly')
                    or isinstance(track, bool) or not isinstance(track, (int, float))
                    or not np.isfinite(track) or int(track) != track or track < 1):
                raise ValueError('Require single-track entity monthly parents with an explicit track')
            if parent.get('flight_direction') not in ('ASCENDING', 'DESCENDING'):
                raise ValueError('Require one explicit orbit direction')
            asset = (str(Path(parent['zarr_path']).resolve()), resolver._group_name(parent))
            if asset in assets:
                raise ValueError('Repeated parent store/group; select each instance once')
            assets.add(asset)
            group = resolver.open_group(parent)
            dataset = datasets[i] if datasets is not None else resources.enter_context(resolver.open(parent))
            times = pd.to_datetime(dataset.time.values, utc=True)
            if not len(times):
                raise ValueError('Monthly parent has no time coordinate entries')
            desc = _describe(parent, group, dataset, times[0], times[-1], window, ['VV_dB', 'VH_dB'])
            if desc['masks']:
                raise ValueError('Select monthly parents before temporal interpolation')
            months = times.strftime('%Y-%m')
            if months.duplicated().any():
                raise ValueError('Parent has multiple values in one calendar month')
            actual_grid = normalize_catalog_record(dict(parent, crs=group.attrs['crs'], transform=group.attrs['transform'],
                                                        width=dataset.sizes['x'], height=dataset.sizes['y']))
            if actual_grid['grid_id'] != parent['grid_id']:
                raise ValueError('Parent catalog grid differs from its actual grid')
            raw = group.attrs.get('processing_variant_json')
            monthly = json.loads(raw) if isinstance(raw, str) else raw
            if (not isinstance(monthly, dict) or monthly.get('contract_version') not in ('cube_derivative_v1', 'cube_derivative_v2')
                    or monthly.get('monthly') not in ('mean', 'median')
                    or monthly.get('monthly_domain') != 'linear_power_from_parent_dB'
                    or monthly.get('temporal_interpolation')):
                raise ValueError('Monthly statistical range/stage is not verifiable; use an explicit derive_cube monthly output')
            if (make_processing_signature(monthly) != parent['processing_signature']
                    or group.attrs.get('processing_signature') != parent['processing_signature']):
                raise ValueError('Parent monthly processing signature differs from its contract')
            reference = group.attrs.get('parent_reference_item', {})
            saved = group.attrs.get('derived_record', {})
            if (monthly.get('parent_track') != track or reference.get('track') != track
                    or reference.get('product_type') != 'scenes'
                    or any(saved.get(k) != parent.get(k) for k in ('track', 'flight_direction', 'geometry_group_id'))
                    or reference.get('flight_direction') != parent['flight_direction']):
                raise ValueError('Parent single-track identity is inconsistent or already mixed')
            interval_start, interval_end = pd.to_datetime([monthly['start_utc'], monthly['end_utc']], utc=True, format='ISO8601')
            selected_ranges = []
            for period in periods:
                left = max(interval_start, period.start_time.tz_localize('UTC'))
                right = min(interval_end, period.end_time.tz_localize('UTC'))
                if left > right:
                    raise ValueError('Requested months exceed a parent statistical range')
                selected_ranges.append([left.isoformat(), right.isoformat()])
            ranges.append(selected_ranges)
            stage = {k: monthly.get(k) for k in ('parent_processing_signature', 'monthly', 'monthly_domain',
                'monthly_validity', 'spatial_filter', 'filter_domain', 'filter_preset', 'filter_support')}
            stages.append(stage)
            descriptions.append(dict(desc, track=int(track), monthly_contract=monthly,
                month_sources={month: str(stamp) for month, stamp in zip(months, times) if month in periods.astype(str)},
                parent_n_obs={'variable': 'n_obs', 'dtype': str(group['n_obs'].dtype),
                              'attrs': dict(group['n_obs'].attrs)} if 'n_obs' in group else None))
    first = parents[0]
    if len({d['track'] for d in descriptions}) != len(parents):
        raise ValueError('Duplicate track variants are not independent inputs')
    if any(p['tile_id'] != first['tile_id'] or p['flight_direction'] != first['flight_direction']
           or p['grid_id'] != first['grid_id'] for p in parents):
        raise ValueError('Parents must share tile, direction and grid; no reprojection is performed')
    if any(stage != stages[0] for stage in stages) or any(r != ranges[0] for r in ranges):
        raise ValueError('Parent statistical ranges or processing stages are not comparable')
    tracks = [d['track'] for d in descriptions]
    auxiliary = {'n_contributing_tracks': 'uint32', **{f'contributes_TK{t}': 'uint8' for t in tracks}}
    return dict(operator_version=MULTITRACK_VERSION, start_month=start_month, end_month=end_month, method=method,
        parents=descriptions, tracks=tracks, grid=descriptions[0]['grid'], window=descriptions[0]['window'],
        bands=['VV_dB', 'VH_dB']+list(auxiliary), masks=[], auxiliary_dtypes=auxiliary,
        parent_version_evidence='generation_and_metadata' if all(d['parent_generation'] is not None for d in descriptions)
                                else 'metadata_only',
        times=periods.to_timestamp().astype(str).tolist(), statistical_ranges_utc=ranges[0],
        periods_utc=[[p.start_time.tz_localize('UTC').isoformat(), (p+1).start_time.tz_localize('UTC').isoformat()]
                     for p in periods],
        time_semantics='UTC month-start labels; periods are start-inclusive/end-exclusive, not acquisition dates',
        domain='paired finite positive linear power from dB; equal track-month weights; output dB',
        provenance='per-track contribution flags; parent n_obs remains at referenced parent month; no pooled n_obs',
        geometry='multiple relative orbits, no incidence-angle normalization')


def multitrack_record(definition):
    contract, parent = definition['contract'], definition['parents'][0]
    signature = definition['processing_signature']
    return normalize_catalog_record(dict(
        item_id=f"{parent['tile_id']}_multitrack_{signature}", tile_id=parent['tile_id'],
        collection_id='s1grits-smonthly', product_type='smonthly',
        product_label=f'smonthly_multitrack_{signature}', processing_level='virtual',
        flight_direction=parent['flight_direction'], track=None, geometry_group_id=None,
        **contract['grid'], bands=contract['bands'], processing_signature=signature,
        product_variant='multitrack_'+signature, processing_variant_json=json.dumps(contract, sort_keys=True),
        datetime=contract['times'][0], start_datetime=contract['times'][0], end_datetime=contract['times'][-1],
        month=contract['start_month']))
