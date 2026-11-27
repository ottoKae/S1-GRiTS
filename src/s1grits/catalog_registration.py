"""Explicit local product references that survive filesystem catalog rebuilds."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from s1grits.atomic_write import atomic_write_json, atomic_write_parquet
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.file_lock import output_lock
from s1grits.resolver import CubeResolver

REGISTRATION_FILE = '.s1grits-registrations.json'


def register_product(catalog_root, record, *, source_root=None) -> dict:
    """Register one complete local Zarr product without copying or changing it.

    Input locators are interpreted against source_root (default: catalog_root).
    The manifest uses paths relative to catalog_root where the filesystem allows.
    Existing item IDs cannot be redirected to another asset or processing identity.
    """
    root = Path(catalog_root).resolve()
    source = CubeResolver(Path(source_root).resolve() if source_root else root)
    record = json.loads(pd.DataFrame([dict(record)]).to_json(orient='records', date_format='iso', date_unit='ns'))[0]
    if record.get('status', 'complete') != 'complete':
        raise ValueError('Only complete products can be registered')
    row = normalize_catalog_record(record)
    row['registration_path'] = REGISTRATION_FILE
    row['zarr_group'] = source._group_name(row)
    for field in ('zarr_path', 'view_path', 'cog_path', 'preview_path'):
        if row.get(field):
            path = source._asset_path(dict(row, view_path=None, zarr_path=row[field])).resolve()
            row[field] = _relative_path(path, root)
    label = row.get('product_label') or row['product_type']
    row['item_path'] = f"items/{label}/{row['item_id']}.json"
    row['collection_id'] = row.get('collection_id') or f"s1grits-{row['product_type']}"
    _validate_record(root, row)
    with output_lock(root):
        registered = load_registered_products(root)
        catalog_path = root/'catalog.parquet'
        catalog = pd.read_parquet(catalog_path) if catalog_path.exists() else pd.DataFrame()
        incoming = _records_frame([row])
        registered = merge_registered_products(root, registered, incoming)
        catalog = merge_registered_products(root, catalog, registered)
        records = json.loads(registered.to_json(orient='records', date_format='iso', date_unit='ns'))
        # The manifest is authoritative; resync can recover after a catalog-write failure.
        atomic_write_json({'version': 1, 'records': records}, root/REGISTRATION_FILE)
        atomic_write_parquet(catalog, catalog_path)
    return next(item for item in records if item['item_id'] == row['item_id'])


def load_registered_products(root: Path) -> pd.DataFrame:
    """Validate explicit references before resync changes any catalogs or STAC."""
    path = root/REGISTRATION_FILE
    if not path.exists():
        return pd.DataFrame()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('records'), list):
        raise ValueError(f'Unsupported registration manifest: {path}')
    records = []
    for record in data['records']:
        if record.get('status') != 'complete':
            raise ValueError('Registered products must be complete')
        row = normalize_catalog_record(record)
        row['registration_path'] = REGISTRATION_FILE
        _validate_record(root, row)
        records.append(row)
    frame = _records_frame(records)
    if not frame.empty and frame.item_id.duplicated().any():
        raise ValueError('Registration manifest contains duplicate item IDs')
    return frame


def merge_registered_products(root: Path, discovered: pd.DataFrame,
                              registered: pd.DataFrame) -> pd.DataFrame:
    """Explicit metadata takes precedence over directory guesses for one asset."""
    if registered.empty:
        return discovered
    resolver = CubeResolver(root)

    def asset(row):
        path = row.get('view_path')
        if pd.notna(path) and path:
            return (str(resolver._asset_path(row).resolve()), 'view')
        path = row.get('zarr_path')
        return (str(resolver._asset_path(row).resolve()), resolver._group_name(row)) if path and not pd.isna(path) else None

    identities = {row['item_id']: row for _, row in registered.iterrows()}
    for _, row in discovered.iterrows():
        replacement = identities.get(row.get('item_id'))
        if replacement is not None:
            if (asset(row) != asset(replacement)
                    or any(row.get(key) != replacement.get(key)
                           for key in ('processing_signature', 'grid_id', 'product_type'))):
                raise ValueError(f"Registered item ID conflicts with existing product: {row['item_id']}")
    assets = {asset(row) for _, row in registered.iterrows()}
    keep = [asset(row) not in assets or (
        pd.notna(row.get('registration_path')) and row.get('registration_path')
        and row.get('item_id') not in identities) for _, row in discovered.iterrows()]
    merged = pd.concat([discovered.loc[keep], registered], ignore_index=True)
    signatures = {}
    for _, row in merged.iterrows():
        key, signature = asset(row), row.get('processing_signature')
        if key is not None and pd.notna(signature):
            signatures.setdefault(key, set()).add(signature)
    if any(len(values) > 1 for values in signatures.values()):
        raise ValueError('One registered path/group cannot have multiple processing signatures')
    return _records_frame(merged)


def _validate_record(root, row):
    for key in ('item_id', 'tile_id', 'product_type', 'processing_signature', 'grid_id'):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f'Registered product requires {key}')
    for key in ('item_id', 'tile_id', 'product_label', 'collection_id'):
        value = row.get(key)
        if value and ('/' in value or '\\' in value or value in ('.', '..')):
            raise ValueError(f'{key} must be a single identifier')
    resolver = CubeResolver(root)
    if row.get('view_path'):
        if row.get('zarr_path'):
            raise ValueError('Choose either a virtual definition or a Zarr asset')
        from s1grits.analysis.views import inspect_view
        actual = inspect_view(resolver._asset_path(row))
        for key in ('item_id', 'product_type', 'grid_id', 'processing_signature', 'bands'):
            if actual[key] != row[key]:
                raise ValueError(f'Registered view metadata differs: {key}')
        return
    group = resolver.open_group(row)
    if group.attrs.get('status', 'complete') != 'complete':
        raise ValueError('Registered store is not complete')
    actual = normalize_catalog_record(dict(row, crs=group.attrs['crs'],
        transform=group.attrs['transform'], width=group['x'].shape[0], height=group['y'].shape[0]))
    if actual['grid_id'] != row['grid_id']:
        raise ValueError('Registered grid metadata differs from the selected store/group')
    signature = group.attrs.get('processing_signature')
    if signature and signature != row['processing_signature']:
        raise ValueError('Registered processing signature differs from the selected store/group')
    if row['product_type'] != 'static' and 'time' not in group:
        raise ValueError('Time-varying registered products require a time coordinate')
    for band in json.loads(row.get('bands') or '[]'):
        if band not in group:
            raise ValueError(f'Registered band is missing: {band}')


def _records_frame(records):
    frame = pd.DataFrame(records)
    for key in ('datetime', 'start_datetime', 'end_datetime'):
        if key in frame:
            frame[key] = pd.to_datetime(frame[key], utc=True, format='mixed').dt.tz_localize(None)
    return frame


def _relative_path(path: Path, root: Path) -> str:
    try:
        return Path(os.path.relpath(path, root)).as_posix()
    except ValueError:  # Windows assets on another drive cannot use relative paths.
        return path.as_posix()
