"""Optional, first-event monitoring through the separately installed public nrt API."""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path

import numpy as np
import pandas as pd
from rasterio.transform import Affine
import xarray as xr
import zarr

from s1grits.asf_io import NODATA_SENTINEL
from s1grits.product_instance import make_processing_signature
from s1grits.resolver import CubeResolver
from s1grits.zarr_encoding import create_zarr_array


def analyze_cusum(catalog_root, item_id, output_root, *, band, history_start, history_end,
                  monitor_start, monitor_end, trend=True, harmonic_order=2,
                  sensitivity=.05, fit_alpha=.05, window=None, chunk_size=64):
    """Read an existing Cube; write independent 2D analysis maps and a lineage receipt.

    Dates are inclusive UTC timestamps. The selected catalog item references its
    entire store. No implicit interpolation, download, reset or parent mutation.
    """
    try:
        backend_version = version('nrt')
    except PackageNotFoundError as exc:
        raise ImportError('CUSUM needs separately installed nrt==0.3.0; see docs/cusum.md') from exc
    if backend_version != '0.3.0':
        raise ImportError('This adapter is verified with nrt==0.3.0; see docs/cusum.md')
    from nrt.monitor.cusum import CuSum

    root, destination = Path(catalog_root).resolve(), Path(output_root).resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('Analysis output must be separate from the source Cube root')
    if destination.exists():
        raise FileExistsError('Use a new analysis output root')
    if band not in ('VV_dB', 'VH_dB'):
        raise ValueError('Select VV_dB or VH_dB')
    if not isinstance(trend, bool) or not isinstance(harmonic_order, int) or harmonic_order < 0:
        raise ValueError('trend must be boolean and harmonic_order a nonnegative integer')
    if not 0 < sensitivity < 1 or not 0 < fit_alpha < 1:
        raise ValueError('sensitivity and fit_alpha must be between zero and one')
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError('chunk_size must be a positive integer')
    dates = pd.DatetimeIndex([pd.to_datetime(value, utc=True)
                             for value in (history_start, history_end, monitor_start, monitor_end)])
    if dates.isna().any() or not dates[0] <= dates[1] < dates[2] <= dates[3]:
        raise ValueError('Provide ordered, nonoverlapping history and monitoring UTC intervals')
    resolver = CubeResolver(root)
    matches = resolver.catalog[resolver.catalog.item_id == item_id]
    if len(matches) != 1:
        raise ValueError('Select one complete, unambiguous catalog item')
    parent = json.loads(matches.to_json(orient='records', date_format='iso', date_unit='ns'))[0]
    if parent['product_type'] not in ('scenes', 'smonthly', 'monthly'):
        raise ValueError('CUSUM requires a time-varying Cube')
    if not parent.get('processing_signature') or not parent.get('grid_id'):
        raise ValueError('Parent processing signature and grid identity are required')
    path = resolver._asset_path(parent)
    if destination.is_relative_to(path.resolve()) or path.resolve().is_relative_to(destination):
        raise ValueError('Analysis output must be separate from the referenced source store')
    group = resolver.open_group(parent)
    with resolver.open(parent, chunks={'time': 1, 'y': chunk_size, 'x': chunk_size}) as ds:
        if band not in ds or set(ds[band].dims) != {'time', 'y', 'x'}:
            raise ValueError('Selected band needs named time/y/x dimensions')
        times = pd.to_datetime(ds.time.values, utc=True, errors='coerce')
        if times.isna().any() or times.has_duplicates or not times.is_monotonic_increasing:
            raise ValueError('Parent timestamps must be valid, unique and increasing')
        history = np.flatnonzero((times >= dates[0]) & (times <= dates[1]))
        monitoring = np.flatnonzero((times >= dates[2]) & (times <= dates[3]))
        if not len(history) or not len(monitoring):
            raise ValueError('Both intervals must contain parent timestamps')
        selected = np.concatenate([history, monitoring])
        days = times[selected].tz_localize(None).values.astype('datetime64[D]').astype('int64')
        if days.min() < 1 or days.max() > np.iinfo('uint16').max:
            raise ValueError('Selected dates exceed the nrt uint16 Unix-day representation')
        height, width = ds.sizes['y'], ds.sizes['x']
        if window is None:
            window = [0, 0, width, height]
        if (len(window) != 4 or any(not isinstance(v, int) for v in window)
                or min(window[:2]) < 0 or min(window[2:]) < 1
                or window[0]+window[2] > width or window[1]+window[3] > height):
            raise ValueError('window must be integer x/y offset and positive width/height inside the parent')
        x0, y0, width, height = window
        native = Affine(*group.attrs['transform'][:6])
        x, y = ds.x.values, ds.y.values
        if (native.b != 0 or native.d != 0 or native.a <= 0 or native.e >= 0
                or not np.allclose(x, native.c+(np.arange(len(x))+.5)*native.a, rtol=0, atol=1e-7)
                or not np.allclose(y, native.f+(np.arange(len(y))+.5)*native.e, rtol=0, atol=1e-7)):
            raise ValueError('Parent coordinates must match its north-up affine grid')
        array = ds[band].transpose('time', 'y', 'x')
        mask_name = band[:2]+'_observed_mask'
        if mask_name in ds and set(ds[mask_name].dims) != {'time', 'y', 'x'}:
            raise ValueError('Observed mask needs named time/y/x dimensions')
        if band[:2]+'_interpolated_mask' in ds and mask_name not in ds:
            raise ValueError('Interpolated input needs its observed mask; select the pre-interpolation Cube')
        observed = ds[mask_name].transpose('time', 'y', 'x') if mask_name in ds else None
        fill = array.encoding.get('_FillValue', array.attrs.get('_FillValue'))

        def read_values(ids, ys, xs):
            values = np.array(array.isel(time=ids, y=ys, x=xs).values, dtype='f4', copy=True)
            invalid = ~np.isfinite(values) | (values == NODATA_SENTINEL)
            if fill is not None and np.isfinite(fill):
                invalid |= values == fill
            if observed is not None:
                invalid |= observed.isel(time=ids, y=ys, x=xs).values != 1
            values[invalid] = np.nan
            return values

        digest = hashlib.sha256(times[selected].asi8.astype('<i8').tobytes())
        for index in selected:
            for row in range(y0, y0+height, 128):
                values = read_values([index], slice(row, min(row+128, y0+height)), slice(x0, x0+width))
                digest.update(values.astype('<f4', copy=False).tobytes())
        contract = dict(contract_version='nrt_cusum_v1', backend='ec-jrc/nrt.CuSum', backend_version=backend_version,
            runtime={name: version(name) for name in ('numpy', 'scipy', 'numba', 's1grits')},
            backend_block_size=chunk_size,
            parent_item_id=item_id, parent_processing_signature=parent['processing_signature'],
            parent_grid_id=parent['grid_id'], parent_generation=group.attrs.get('generation_id'),
            parent_track=parent.get('track'), parent_values_sha256=digest.hexdigest(),
            crs=group.attrs['crs'], transform=list(native*Affine.translation(x0, y0))[:6],
            window=list(window), band=band, domain='dB', intervals_utc=dates.astype(str).tolist(),
            input_times_utc=times[selected].astype(str).tolist(), observed_mask=mask_name if observed is not None else None,
            interpolation='none', fit_method='ROC', fit_alpha=fit_alpha, trend=trend,
            harmonic_order=harmonic_order, sensitivity=sensitivity, update_mask=True,
            minimum_history=max(1+int(trend)+2*harmonic_order+2,
                                int(np.ceil(1.5*(1+int(trend)+2*harmonic_order)))),
            unusable_fit='exclude_from_monitoring_and_clear_diagnostics',
            monitoring='stop_after_first_confirmed_change')
        signature = make_processing_signature(contract)
        destination.mkdir(parents=True)
        output = zarr.open_group(str(destination/'cusum.zarr'), mode='w', zarr_format=3)
        output.attrs.update(status='building', processing_signature=signature, contract=contract,
                            crs=contract['crs'], transform=contract['transform'])
        for name, values in [('x', x[x0:x0+width]), ('y', y[y0:y0+height])]:
            create_zarr_array(output, name, data=values, dimension_names=[name])
        fields = {'backend_mask': 'u1', 'fit_usable': 'u1', 'detection_date': 'i4',
                  'process': 'f8', 'boundary': 'f8', 'sigma': 'f8', 'fit_start': 'i4',
                  'history_used': 'u4', 'observations_used': 'u4',
                  'history_available': 'u4', 'monitoring_available': 'u4'}
        for name, dtype in fields.items():
            create_zarr_array(output, name, shape=(height, width), chunks=(chunk_size, chunk_size),
                              dtype=dtype, fill_value=np.nan if dtype == 'f8' else 0, dimension_names=['y', 'x'])
        output['backend_mask'].attrs['codes'] = {'0': 'not_monitored', '1': 'active',
            '2': 'unstable_history', '3': 'confirmed_change', '4': 'insufficient_observations'}
        for name in ('detection_date', 'fit_start'):
            output[name].attrs['description'] = 'Integer Unix days; 0 means unavailable. See backend_mask and fit_usable.'
        counts = np.zeros(5, dtype='int64')
        for row in range(0, height, chunk_size):
            for col in range(0, width, chunk_size):
                ys, xs = slice(row, min(row+chunk_size, height)), slice(col, min(col+chunk_size, width))
                values = read_values(selected, slice(y0+ys.start, y0+ys.stop), slice(x0+xs.start, x0+xs.stop))
                result = _monitor_block(CuSum, values, times[selected], len(history), trend, harmonic_order,
                                        sensitivity, fit_alpha, contract['minimum_history'])
                for name, values in result.items():
                    output[name][ys, xs] = values
                counts += np.bincount(result['backend_mask'].ravel(), minlength=5)
        receipt = dict(status='complete', processing_signature=signature, contract=contract,
                       parent_reference_item=parent, parent_store=str(path.resolve()),
                       status_counts={str(i): int(n) for i, n in enumerate(counts)}, output='cusum.zarr')
        output.attrs['status'] = 'complete'
        (destination/'analysis.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        return receipt


def _monitor_block(backend, values, times, split, trend, harmonics, sensitivity, alpha, minimum):
    import numba

    history = np.isfinite(values[:split]).sum(axis=0)
    monitoring = np.isfinite(values[split:]).sum(axis=0)
    mask = np.where(history+monitoring == 0, 0, 1).astype('u1')
    # ROC needs residuals beyond its initial recursive fit; nrt's 1.5x rule alone can divide by zero.
    mask[(mask == 1) & (history < minimum)] = 4
    result = {name: np.zeros(mask.shape, dtype='u4') for name in
              ('fit_usable', 'detection_date', 'fit_start', 'history_used', 'observations_used')}
    result.update({name: np.full(mask.shape, np.nan) for name in ('process', 'boundary', 'sigma')})
    result.update(backend_mask=mask, history_available=history, monitoring_available=monitoring)
    if not np.any(mask == 1):
        return result
    model = backend(trend=trend, harmonic_order=harmonics, sensitivity=sensitivity,
                    mask=mask, save_fit_start=True)
    data = xr.DataArray(values[:split], dims=['time', 'y', 'x'],
                        coords={'time': times[:split].tz_localize(None).values,
                                'y': np.arange(mask.shape[0]), 'x': np.arange(mask.shape[1])})
    previous_threads = numba.get_num_threads()
    try:
        model.fit(data, method='ROC', alpha=alpha, n_threads=1)
    finally:
        numba.set_num_threads(previous_threads)
    usable = (model.mask == 1) & np.isfinite(model.sigma) & (model.sigma > 0) & np.isfinite(model.beta).all(axis=0)
    for value, date in zip(values[split:], times[split:]):
        model.monitor(np.where(usable, value, np.nan), date.tz_localize(None).to_pydatetime())
    result.update(backend_mask=model.mask, fit_usable=usable.astype('u1'), detection_date=model.detection_date,
                  process=model.process, boundary=model.boundary, sigma=model.sigma, fit_start=model.fit_start,
                  history_used=model.histsize, observations_used=model.n)
    # nrt initializes unused residuals to zero, so its inactive counts are not valid support counts.
    for name in ('process', 'boundary', 'sigma'):
        result[name] = np.where(usable, result[name], np.nan)
    for name in ('fit_start', 'history_used', 'observations_used'):
        result[name] = np.where(usable, result[name], 0)
    return result
