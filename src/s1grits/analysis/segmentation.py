"""Single-pixel segmentation with local LandTrendr or explicit Earth Engine."""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path

import numpy as np
import pandas as pd
from rasterio.transform import Affine

from s1grits.asf_io import NODATA_SENTINEL
from s1grits.product_instance import make_processing_signature
from s1grits.resolver import CubeResolver


def analyze_segments(catalog_root, item_id, output_root, *, algorithm, backend='local', band, pixel,
                     start, end, project=None, annual_method=None, annual_domain=None,
                     disturbance_direction=None, min_annual_observations=1):
    """Analyze one native-grid (column, row); Earth Engine is explicitly selected.

    The first version uses recorded algorithm presets and an independent result
    directory. LandTrendr requires explicit annual aggregation and sign choices.
    """
    if backend not in ('local', 'earthengine') or algorithm not in ('ccdc', 'landtrendr'):
        raise ValueError('Select backend=local/earthengine and algorithm=ccdc/landtrendr')
    if backend == 'local' and algorithm == 'ccdc':
        raise ValueError('Local CCDC is not implemented; Earth Engine requires explicit backend=earthengine')
    if backend == 'local' and project is not None:
        raise ValueError('project applies only to backend=earthengine')
    if algorithm == 'landtrendr':
        if annual_method not in ('mean', 'median') or annual_domain not in ('db', 'linear_power'):
            raise ValueError('LandTrendr requires annual_method=mean/median and annual_domain=db/linear_power')
        if disturbance_direction not in ('increase', 'decrease'):
            raise ValueError('LandTrendr requires disturbance_direction=increase/decrease')
        if not isinstance(min_annual_observations, int) or min_annual_observations < 1:
            raise ValueError('min_annual_observations must be a positive integer')
    elif any(v is not None for v in (annual_method, annual_domain, disturbance_direction)) or min_annual_observations != 1:
        raise ValueError('Annual aggregation and direction settings apply only to LandTrendr')
    frame, parent, lineage, destination = _read_point(catalog_root, item_id, output_root, band, pixel, start, end)
    distribution = 'landtrendr' if backend == 'local' else 'earthengine-api'
    try:
        client_version = version(distribution)
    except PackageNotFoundError as exc:
        raise ImportError(f'Install the optional {distribution} backend; see docs/segmentation.md') from exc
    if backend == 'local' and client_version != '0.4.0':
        raise ImportError('Local LandTrendr is verified with nthh/LT-rs landtrendr==0.4.0; see docs/segmentation.md')
    sign = -1 if disturbance_direction == 'decrease' else 1
    if algorithm == 'landtrendr':
        model_input = _annual_series(frame, start, end, annual_method, annual_domain, min_annual_observations)
        model_input['signal'] = sign*model_input.value_db
        parameters = dict(maxSegments=6, spikeThreshold=.9, vertexCountOvershoot=3,
            preventOneYearRecovery=False, recoveryThreshold=.25, pvalThreshold=.1,
            bestModelProportion=.75, minObservationsNeeded=6)
        if backend == 'local':
            # LT-rs expects disturbance DOWN; GEE and the result tables use UP.
            model_input['signal'] = -sign*model_input.value_db
            parameters = dict(max_segments=6, spike_threshold=.9, vertex_count_overshoot=3,
                prevent_one_year_recovery=False, recovery_threshold=.25, p_value_threshold=.1,
                best_model_proportion=.75, min_observations_needed=6)
    else:
        model_input = frame.assign(signal=frame.value_db)
        parameters = dict(minObservations=6, chiSquareProbability=.99,
            minNumOfYearsScaler=1.33, dateFormat=2, maxIterations=25000,
            **{'lambda': 0, 'breakpointBands': ['signal'], 'tmaskBands': None})
    contract = dict(contract_version='ee_point_segmentation_v1', algorithm=algorithm, backend=backend,
        client_version=client_version, service_version='managed_unpinned', input=lineage,
        parameters=parameters, annual_method=annual_method, annual_domain=annual_domain,
        min_annual_observations=min_annual_observations if algorithm == 'landtrendr' else None,
        disturbance_direction=disturbance_direction, model_value_domain='dB', signal_multiplier=sign,
        interpolation='none', transmission='valid_values_and_UTC_millisecond_dates_at_virtual_point')
    if backend == 'local':
        contract.update(contract_version='local_lt_point_v1', implementation='nthh/LT-rs.pixel',
            implementation_status='experimental', service_version=None,
            backend_version=client_version, numpy_version=version('numpy'),
            signal_multiplier=-sign, result_direction_multiplier=sign,
            model_dtype='float32', year_dtype='int32', transmission='none',
            missing_years='omitted_from_backend_and_fitted_table')
    signature = make_processing_signature(contract)
    valid = model_input[np.isfinite(model_input.signal)]
    if backend == 'local' and len(valid) > 128:
        raise ValueError('LT-rs 0.4.0 supports at most 128 valid annual observations')
    receipt = dict(status='building', processing_signature=signature, contract=contract,
        parent_reference_item=parent, selected_observations=len(frame), valid_observations=int(frame.value_db.notna().sum()),
        model_observations=len(valid), project=project, project_selection='explicit' if project else 'credential_default',
        started_utc=pd.Timestamp.now(tz='UTC').isoformat())
    if backend == 'local':
        receipt['project_selection'] = 'not_applicable'
    destination.mkdir(parents=True)
    frame.to_parquet(destination/'input.parquet', index=False)
    model_input.to_parquet(destination/'model_input.parquet', index=False)
    receipt_path = destination/'analysis.json'
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    try:
        if len(valid) < 6:
            raw, state = {}, 'insufficient_observations'
        elif backend == 'local':
            import landtrendr

            values = np.ascontiguousarray(valid.signal, dtype='f4')
            years = np.ascontiguousarray(valid.year, dtype='i4')
            fitted_values, vertices, rmse = landtrendr.pixel(values, years, **parameters)
            raw = dict(years=years.tolist(), signal=values.tolist(), fitted=fitted_values.tolist(),
                       is_vertex=vertices.tolist(), rmse=float(rmse))
            state = 'fitted' if np.count_nonzero(vertices) >= 2 else 'no_fit'
        else:
            import ee

            ee.Initialize(project=project)
            images = [ee.Image.constant(float(row.signal)).toDouble().rename('signal')
                      .set('system:time_start', int(row.time.value//1_000_000)) for row in valid.itertuples()]
            collection = ee.ImageCollection.fromImages(images)
            if algorithm == 'ccdc':
                result = ee.Algorithms.TemporalSegmentation.Ccdc(collection=collection, **parameters)
            else:
                result = ee.Algorithms.TemporalSegmentation.LandTrendr(timeSeries=collection, **parameters)
            raw = result.reduceRegion(ee.Reducer.first(), ee.Geometry.Point([15, -15], 'EPSG:3857'),
                                      crs='EPSG:3857', scale=30, maxPixels=16).getInfo()
            support = raw.get('tStart') if algorithm == 'ccdc' else (raw.get('LandTrendr') or [[]])[0]
            state = 'fitted' if support else 'no_fit'
        (destination/'raw_response.json').write_text(json.dumps(raw, indent=2), encoding='utf-8')
        table_input = raw
        if backend == 'local' and state == 'fitted':
            table_input = dict(LandTrendr=[raw['years'], (-values).tolist(),
                               (-fitted_values).tolist(), raw['is_vertex']], rmse=raw['rmse'])
        elif backend == 'local':
            table_input = {}
        segments, fitted = _result_tables(table_input, algorithm, sign)
        segments.to_parquet(destination/'segments.parquet', index=False)
        if fitted is not None:
            fitted.to_parquet(destination/'annual_fitted.parquet', index=False)
        receipt.update(status='complete', analysis_state=state, segments=len(segments),
                       completed_utc=pd.Timestamp.now(tz='UTC').isoformat())
        if algorithm == 'ccdc':
            receipt['confirmed_breaks'] = int(segments.confirmed.sum()) if state == 'fitted' else None
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        return receipt
    except Exception as exc:
        receipt.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        raise


def _read_parent(catalog_root, item_id, output_root):
    root, destination = Path(catalog_root).resolve(), Path(output_root).resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('Analysis output must be separate from the source Cube root')
    if destination.exists():
        raise FileExistsError('Use a new analysis output root')
    resolver = CubeResolver(root)
    matches = resolver.catalog[resolver.catalog.item_id == item_id]
    if len(matches) != 1:
        raise ValueError('Select one complete, unambiguous catalog item')
    parent = json.loads(matches.to_json(orient='records', date_format='iso', date_unit='ns'))[0]
    if parent['product_type'] not in ('scenes', 'monthly', 'smonthly'):
        raise ValueError('Segmentation requires a time-varying Cube')
    if not parent.get('processing_signature') or not parent.get('grid_id'):
        raise ValueError('Parent processing signature and grid identity are required')
    path = resolver._asset_path(parent)
    if destination.is_relative_to(path.resolve()) or path.resolve().is_relative_to(destination):
        raise ValueError('Analysis output must be separate from the referenced source store')
    return resolver, parent, path, destination


def _read_point(catalog_root, item_id, output_root, band, pixel, start, end):
    if band not in ('VV_dB', 'VH_dB'):
        raise ValueError('Select VV_dB or VH_dB')
    if len(pixel) != 2 or any(not isinstance(v, int) or v < 0 for v in pixel):
        raise ValueError('pixel must contain nonnegative integer column and row')
    start, end = pd.to_datetime(start, utc=True), pd.to_datetime(end, utc=True)
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError('Provide a valid inclusive UTC start/end interval')
    resolver, parent, path, destination = _read_parent(catalog_root, item_id, output_root)
    group = resolver.open_group(parent)
    with resolver.open(parent) as ds:
        if band not in ds or set(ds[band].dims) != {'time', 'y', 'x'}:
            raise ValueError('Selected band needs named time/y/x dimensions')
        col, row = pixel
        if col >= ds.sizes['x'] or row >= ds.sizes['y']:
            raise ValueError('pixel must be inside the parent grid')
        times = pd.to_datetime(ds.time.values, utc=True, errors='coerce')
        if times.isna().any() or times.has_duplicates or not times.is_monotonic_increasing:
            raise ValueError('Parent timestamps must be valid, unique and increasing')
        ids = np.flatnonzero((times >= start) & (times <= end))
        if not len(ids):
            raise ValueError('No parent timestamps in the selected interval')
        array = ds[band]
        values = np.array(array.isel(time=ids, y=row, x=col).values, dtype='f8', copy=True)
        invalid = ~np.isfinite(values) | (values == NODATA_SENTINEL)
        fill = array.encoding.get('_FillValue', array.attrs.get('_FillValue'))
        if fill is not None and np.isfinite(fill):
            invalid |= values == fill
        mask_name = band[:2]+'_observed_mask'
        if band[:2]+'_interpolated_mask' in ds and mask_name not in ds:
            raise ValueError('Interpolated input requires its observed mask')
        if mask_name in ds:
            if set(ds[mask_name].dims) != {'time', 'y', 'x'}:
                raise ValueError('Observed mask needs named time/y/x dimensions')
            invalid |= ds[mask_name].isel(time=ids, y=row, x=col).values != 1
        values[invalid] = np.nan
        native = Affine(*group.attrs['transform'][:6])
        center = [float(ds.x.values[col]), float(ds.y.values[row])]
        if not np.allclose(center, native*(col+.5, row+.5), rtol=0, atol=1e-7):
            raise ValueError('Parent pixel coordinates must match its affine grid')
        lineage = dict(parent_item_id=item_id, parent_processing_signature=parent['processing_signature'],
            parent_grid_id=parent['grid_id'], parent_generation=group.attrs.get('generation_id'),
            parent_track=parent.get('track'), crs=group.attrs['crs'], transform=list(native)[:6],
            pixel=list(pixel), pixel_center=center, band=band, start_utc=start.isoformat(), end_utc=end.isoformat(),
            observed_mask=mask_name if mask_name in ds else None,
            effective_values_sha256=hashlib.sha256(times[ids].asi8.astype('<i8').tobytes()+values.astype('<f8').tobytes()).hexdigest())
    return pd.DataFrame({'time': times[ids], 'value_db': values}), parent, lineage, destination


def _annual_series(frame, start, end, method, domain, minimum):
    rows = []
    for year in range(pd.to_datetime(start, utc=True).year, pd.to_datetime(end, utc=True).year+1):
        values = frame.loc[frame.time.dt.year == year, 'value_db'].dropna().to_numpy()
        value = np.nan
        if len(values) >= minimum:
            if domain == 'linear_power':
                values = np.power(10., values/10)
            value = float(np.mean(values) if method == 'mean' else np.median(values))
            if domain == 'linear_power':
                value = float(10*np.log10(value))
        rows.append(dict(time=pd.Timestamp(year=year, month=7, day=1, tz='UTC'), year=year,
                         observations=len(values), value_db=value))
    return pd.DataFrame(rows)


def _result_tables(raw, algorithm, sign):
    if algorithm == 'ccdc':
        fields = ['t_start_ms', 't_end_ms', 't_break_ms', 'change_prob', 'observations',
                  'magnitude_db', 'rmse_db', 'coefficients', 'confirmed']
        rows = []
        for i, start in enumerate(raw.get('tStart') or []):
            stop, change, probability = raw['tEnd'][i], raw['tBreak'][i], raw['changeProb'][i]
            rows.append([start, stop, change, probability, raw['numObs'][i], raw['signal_magnitude'][i],
                         raw['signal_rmse'][i], raw['signal_coefs'][i], change > 0 and probability == 1])
        return pd.DataFrame(rows, columns=fields), None
    matrix = raw.get('LandTrendr')
    fitted = pd.DataFrame(np.array(matrix).T if matrix else [],
                          columns=['year', 'observed_directed_db', 'fitted_directed_db', 'is_vertex'])
    fitted['observed_db'] = sign*fitted.observed_directed_db
    fitted['fitted_db'] = sign*fitted.fitted_directed_db
    vertices = fitted[fitted.is_vertex == 1]
    rows = []
    for first, last in zip(vertices.iloc[:-1].itertuples(), vertices.iloc[1:].itertuples()):
        rows.append(dict(start_year=int(first.year), end_year=int(last.year),
            start_db=first.fitted_db, end_db=last.fitted_db, delta_db=last.fitted_db-first.fitted_db,
            delta_disturbance_db=last.fitted_directed_db-first.fitted_directed_db,
            duration_years=int(last.year-first.year), rmse_db=raw['rmse']))
    return pd.DataFrame(rows, columns=['start_year', 'end_year', 'start_db', 'end_db',
        'delta_db', 'delta_disturbance_db', 'duration_years', 'rmse_db']), fitted
