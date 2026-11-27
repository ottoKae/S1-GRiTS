"""Local selection/features and monthly track views with explicit materialization."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import uuid
import warnings

import numpy as np
import pandas as pd
import xarray as xr
from rasterio.transform import Affine

from s1grits.analysis.cube_ops import clean_backscatter, dual_pol_features, mask_values, parent_masks, select_parent
from s1grits.analysis.multitrack import MULTITRACK_VERSION, describe_multitrack, multitrack_record, open_multitrack
from s1grits.atomic_write import atomic_write_json, atomic_write_parquet
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.product_instance import make_processing_signature
from s1grits.resolver import CubeResolver
from s1grits.scenes.blocks import (
    _begin_zarr_timestep_blockwise, _finalize_zarr_timestep_blockwise, _iter_spatial_blocks,
    _rollback_zarr_timestep_blockwise, _write_smonthly_block_bands,
)
from s1grits.scenes.store import _init_zarr_2band
from s1grits.zarr_encoding import create_zarr_array

VIEW_VERSION = 's1grits_view_v1'


def define_view(catalog_root, item_id, *, start, end, window=None,
                bands=('VV_dB', 'VH_dB', 'Ratio', 'RVI')) -> dict:
    """Describe a view without reading backscatter chunks or writing files."""
    resolver = CubeResolver(catalog_root)
    matches = resolver.catalog[resolver.catalog.item_id == item_id]
    if len(matches) != 1:
        raise ValueError('Select one complete, unambiguous parent item')
    parent = json.loads(matches.to_json(orient='records', date_format='iso', date_unit='ns'))[0]
    if parent.get('view_path'):
        raise ValueError('Views require an entity parent; nested views are not supported')
    parent['zarr_path'] = str(resolver._asset_path(parent).resolve())
    group = resolver.open_group(parent)
    with resolver.open(parent) as dataset:
        contract = _describe(parent, group, dataset, start, end, window, bands)
    return {'version': VIEW_VERSION, 'contract': contract,
            'processing_signature': make_processing_signature(contract), 'parent': parent}


def register_view(catalog_root, definition) -> dict:
    """Save a definition and register its virtual representation, without a Zarr output."""
    from s1grits.catalog_registration import _relative_path, register_product

    root = Path(catalog_root).resolve()
    definition = copy.deepcopy(definition)
    parents = definition['parents'] if definition.get('version') == MULTITRACK_VERSION else [definition['parent']]
    path = root/'views'/f"{definition['processing_signature']}.json"
    for parent in parents:
        parent_path = Path(parent['zarr_path'])
        if not parent_path.is_absolute():
            raise ValueError('register_view requires the absolute parent locator returned by define_view')
        if path.is_relative_to(parent_path.resolve()):
            raise ValueError('View definition must be separate from its parent store')
    _check_definition(definition)
    for parent in parents:
        parent['zarr_path'] = _relative_path(Path(parent['zarr_path']), path.parent)
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))['contract'] != definition['contract']:
            raise FileExistsError('Existing view scientific definition differs; use a new output location')
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(definition, path)
    record = _view_record(definition)
    record['view_path'] = path.relative_to(root).as_posix()
    return register_product(root, record)


def inspect_view(path) -> dict:
    """Check definition identity and available parent-version evidence; no pixel calculation."""
    definition = _load_definition(path)
    _check_definition(definition)
    return _view_record(definition)


def open_view(path, *, chunks=None) -> xr.Dataset:
    """Open a Dask-backed view. Values compute only when requested by the caller."""
    definition = _load_definition(path)
    if definition.get('version') == MULTITRACK_VERSION:
        return open_multitrack(definition, chunks=chunks)
    parent, contract = definition['parent'], definition['contract']
    resolver = CubeResolver(Path(path).parent)
    dataset = resolver.open(parent, chunks=chunks if chunks is not None else {})
    try:
        _check_definition(definition, dataset=dataset)
        if contract['parent_version_evidence'] == 'metadata_only':
            warnings.warn('Parent has no generation_id: metadata checks cannot detect in-place value changes',
                          UserWarning, stacklevel=2)
        x0, y0, width, height = contract['window']
        selected = dataset.sel(time=pd.to_datetime(contract['times'], utc=True).tz_localize(None).values
                              ).isel(y=slice(y0, y0+height), x=slice(x0, x0+width))
        cleaned = {}
        for name in ('VV_dB', 'VH_dB'):
            array = selected[name].transpose('time', 'y', 'x')
            fill = array.encoding.get('_FillValue', array.attrs.get('_FillValue'))
            cleaned[name] = xr.apply_ufunc(clean_backscatter, array, kwargs={'fill': fill},
                                          dask='parallelized', output_dtypes=[np.float32])
        values = {}
        for name in contract['bands']:
            if name in cleaned:
                values[name] = cleaned[name]
            else:
                values[name] = xr.apply_ufunc(
                    lambda a, b, feature: dual_pol_features(a, b, [feature.lower()])[feature],
                    cleaned['VV_dB'], cleaned['VH_dB'], kwargs={'feature': name},
                    dask='parallelized', output_dtypes=[np.float32])
            values[name].attrs = {'units': 'dB' if name.endswith('_dB') else '1'}
        for name in contract['masks']:
            values[name] = xr.apply_ufunc(mask_values, selected[name].transpose('time', 'y', 'x'),
                                         dask='parallelized', output_dtypes=[np.uint8], keep_attrs=True)
        record = _view_record(definition)
        result = xr.Dataset(values, attrs={
            'item_id': record['item_id'],
            'crs': record['crs'], 'transform': record['transform'], 'grid_id': record['grid_id'],
            'processing_signature': record['processing_signature'], 'representation': 'virtual',
            'parent_version_evidence': contract['parent_version_evidence'], 'view_definition': definition,
        })
        result.set_close(dataset.close)
        return result
    except Exception:
        dataset.close()
        raise


def materialize_view(catalog_root, item_id, output_root, *, chunk_size=256) -> dict:
    """Explicitly compute one registered view into a new, independently readable Cube."""
    from s1grits.catalog_registration import _records_frame, register_product

    root, destination = Path(catalog_root).resolve(), Path(output_root).resolve()
    resolver = CubeResolver(root)
    matches = resolver.catalog[resolver.catalog.item_id == item_id]
    if len(matches) != 1 or not matches.iloc[0].get('view_path'):
        raise ValueError('Select one registered virtual view item')
    path = resolver._asset_path(matches.iloc[0])
    definition = _load_definition(path)
    parents = definition['parents'] if definition.get('version') == MULTITRACK_VERSION else [definition['parent']]
    parent_paths = [Path(parent['zarr_path']).resolve() for parent in parents]
    if root.is_relative_to(destination) or any(p.is_relative_to(destination) or destination.is_relative_to(p) for p in parent_paths):
        raise ValueError('Materialized output must be separate from its source assets')
    if destination.exists():
        raise FileExistsError('Use a new output root; existing products are never overwritten')
    if any((p/'zarr.json').exists() or (p/'.zgroup').exists() for p in destination.parents):
        raise ValueError('Materialized output cannot be inside an existing Zarr store')
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError('chunk_size must be a positive integer')
    with resolver.open(matches.iloc[0], chunks={'time': 1, 'y': chunk_size, 'x': chunk_size}) as dataset:
        record = _view_record(definition)
        record.update(item_id=record['item_id']+'_materialized', processing_level='derived',
                      product_label=f"{record['product_type']}_derived_view_{record['processing_signature']}")
        tile_dir = destination/record['tile_id']
        store = tile_dir/record['product_label']/'zarr'/'view.zarr'
        names = list(dataset.data_vars)
        auxiliary = {name: 'uint8' for name in definition['contract']['masks']}
        auxiliary.update(definition['contract'].get('auxiliary_dtypes', {}))
        store.parent.mkdir(parents=True)
        output = _init_zarr_2band(store, dataset.x.values, dataset.y.values, record['crs'],
            Affine(*record['transform']), chunk_size, chunk_size, 'view',
            band_names=[name for name in names if name not in auxiliary])
        for name, dtype in auxiliary.items():
            create_zarr_array(output, name, shape=(0, record['height'], record['width']),
                chunks=(1, chunk_size, chunk_size), dtype=dtype, fill_value=0,
                dimension_names=['time', 'y', 'x'])
        for name in names:
            output[name].attrs.update(dict(dataset[name].attrs, _ARRAY_DIMENSIONS=['time', 'y', 'x']))
        output.attrs.update(status='building', processing_signature=record['processing_signature'],
                            view_definition=dataset.attrs['view_definition'],
                            parent_version_evidence=definition['contract']['parent_version_evidence'])
        if 'time_semantics' in dataset.attrs:
            output.attrs['time_semantics'] = dataset.attrs['time_semantics']
        output.attrs.update({key: record[key] for key in (
            'grid_id', 'product_type', 'product_label', 'product_variant',
            'processing_variant_json', 'geometry_group_id')})
        try:
            for stamp in dataset.time.values:
                index, key = _begin_zarr_timestep_blockwise(output, stamp, names)
                try:
                    for ys, xs in _iter_spatial_blocks(record['height'], record['width'], chunk_size, chunk_size):
                        block = dataset.sel(time=stamp).isel(y=ys, x=xs).compute()
                        _write_smonthly_block_bands(output, index, ys, xs, names,
                                                   {name: block[name].values for name in names})
                    _finalize_zarr_timestep_blockwise(output, index, key)
                except Exception:
                    _rollback_zarr_timestep_blockwise(output, index, names)
                    raise
        except Exception as error:
            output.attrs.update(status='failed', error=str(error))
            raise
        record['zarr_path'] = store.relative_to(tile_dir).as_posix()
        record['item_path'] = f"items/{record['product_label']}/{record['item_id']}.json"
        output.attrs.update(status='complete', derived_record=record, generation_id=uuid.uuid4().hex)
        frame = _records_frame([record])
        atomic_write_parquet(frame, tile_dir/'catalog.parquet')
        atomic_write_parquet(frame, destination/'catalog.parquet')
    if destination.is_relative_to(root):
        register_product(root, record, source_root=destination)
    return record


def _describe(parent, group, dataset, start, end, window, bands):
    if parent.get('view_path') or parent.get('product_type') not in ('scenes', 'smonthly', 'monthly'):
        raise ValueError('Views require an entity time-varying VV/VH parent')
    if not parent.get('processing_signature') or not parent.get('grid_id'):
        raise ValueError('Parent processing signature and grid identity are required')
    if group.attrs.get('status', 'complete') != 'complete':
        raise ValueError('Parent store is not complete')
    if (not isinstance(bands, (list, tuple)) or not bands or len(set(bands)) != len(bands)
            or set(bands)-{'VV_dB', 'VH_dB', 'Ratio', 'RVI'}):
        raise ValueError('Select distinct bands from VV_dB, VH_dB, Ratio and RVI')
    start, end = pd.to_datetime(start, utc=True), pd.to_datetime(end, utc=True)
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError('Provide a valid inclusive UTC start/end interval')
    times, indices, window, native = select_parent(dataset, group.attrs, start, end, window)
    masks = list(parent_masks(dataset))
    # Mixed 2D/3D variables may be enumerated in different orders by Zarr.
    dimensions = list(dataset.VV_dB.dims) + sorted(set(dataset.sizes) - set(dataset.VV_dB.dims))
    evidence = {'sizes': {name: dataset.sizes[name] for name in dimensions}, 'times_ns': times.asi8.tolist(),
        'crs': group.attrs['crs'], 'transform': group.attrs['transform'],
        'processing_signature': group.attrs.get('processing_signature'),
        'generation_id': group.attrs.get('generation_id'),
        'arrays': {name: {'dtype': str(group[name].dtype), 'fill': dict(group[name].attrs).get('_FillValue')}
                   for name in ['VV_dB', 'VH_dB']+masks}}
    x0, y0, width, height = window
    return {'operator_version': VIEW_VERSION,
        'parent_identity': {key: parent.get(key) for key in (
            'item_id', 'product_type', 'processing_signature', 'grid_id', 'track', 'flight_direction', 'geometry_group_id')},
        'parent_metadata_signature': make_processing_signature(evidence),
        'parent_version_evidence': 'generation_and_metadata' if evidence['generation_id'] is not None else 'metadata_only',
        'parent_generation': evidence['generation_id'], 'start': start.isoformat(), 'end': end.isoformat(),
        'window': window, 'bands': list(bands), 'masks': masks,
        'mask_semantics': 'preserved_per_parent_band; no joint feature observation mask',
        'domain': 'VV/VH dB; Ratio/RVI from paired finite positive linear power',
        'dtype': 'float32; masks uint8', 'nodata': 'nonfinite/declared fill/-9999 become NaN; valid zero retained',
        'grid': {'crs': group.attrs['crs'], 'transform': list(native*Affine.translation(x0, y0))[:6],
                 'width': width, 'height': height},
        'times': times[indices].astype(str).tolist()}


def _load_definition(path):
    path = Path(path)
    definition = json.loads(path.read_text(encoding='utf-8'))
    parents = definition['parents'] if definition.get('version') == MULTITRACK_VERSION else [definition['parent']]
    for parent in parents:
        parent['zarr_path'] = str((path.parent/parent['zarr_path']).resolve())
    return definition


def _check_definition(definition, dataset=None):
    if (definition.get('version') not in (VIEW_VERSION, MULTITRACK_VERSION)
            or make_processing_signature(definition['contract']) != definition.get('processing_signature')):
        raise ValueError('Invalid view version or scientific signature')
    if definition['version'] == MULTITRACK_VERSION:
        contract = definition['contract']
        actual = describe_multitrack(definition['parents'], contract['start_month'], contract['end_month'],
                                    contract['method'], contract['window'])
        if actual != contract:
            raise ValueError('Parent version or metadata changed; define a new view')
        return
    parent, contract = definition['parent'], definition['contract']
    resolver = CubeResolver('.')
    group = resolver.open_group(parent)
    own_dataset = dataset is None
    if own_dataset:
        dataset = resolver.open(parent)
    try:
        actual = _describe(parent, group, dataset, contract['start'], contract['end'], contract['window'], contract['bands'])
        if actual != contract:
            raise ValueError('Parent version or metadata changed; define a new view')
    finally:
        if own_dataset:
            dataset.close()


def _view_record(definition):
    if definition.get('version') == MULTITRACK_VERSION:
        return multitrack_record(definition)
    contract, parent = definition['contract'], definition['parent']
    signature = definition['processing_signature']
    times = contract['times']
    return normalize_catalog_record(dict(
        item_id=f"{parent['tile_id']}_view_{signature}", tile_id=parent['tile_id'],
        collection_id=f"s1grits-{parent['product_type']}", product_type=parent['product_type'],
        product_label=f"{parent['product_type']}_view_{signature}", processing_level='virtual',
        flight_direction=parent.get('flight_direction'), track=parent.get('track'),
        geometry_group_id=parent.get('geometry_group_id'), **contract['grid'],
        bands=contract['bands']+contract['masks'], processing_signature=signature,
        product_variant='view_'+signature, processing_variant_json=json.dumps(contract, sort_keys=True),
        datetime=times[0], start_datetime=times[0], end_datetime=times[-1], month=times[0][:7]))
