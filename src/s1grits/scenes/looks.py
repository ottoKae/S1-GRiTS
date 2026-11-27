"""Explicit, pinned RTC-STATIC weights for paired intra-pass mosaicking."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np
import rasterio

from s1grits.lazy_burst import LazyBurstArray
from s1grits.scenes.mosaic import _mosaic_align_scene_window


def load_looks_assets(directory) -> dict:
    """Open a local collection once; content hashes pin its processing identity."""
    root = Path(directory)
    files = sorted(root.glob('*_number_of_looks.tif'))
    if not files:
        raise ValueError('nlooks_weighted requires a nonempty local looks_directory')
    assets = {}
    for path in files:
        match = re.fullmatch(
            r'(OPERA_L2_RTC-S1-STATIC_(T\d{3}-\d{6}-IW[123])_\d{8}_S1[AB]_30_v1\.0)_number_of_looks\.tif',
            path.name)
        if match is None:
            raise ValueError(f'Unsupported RTC-STATIC v1.0 looks filename: {path.name}')
        product, burst = match.groups()
        if burst in assets:
            raise ValueError(f'Ambiguous static weights for {burst}; pin one asset per burst')
        with path.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        with rasterio.open(path) as src:
            if src.count != 1 or src.crs is None:
                raise ValueError(f'Looks asset must be a georeferenced single-band raster: {path}')
            assets[burst] = {
                'array': LazyBurstArray(path.resolve(), 1, src.shape, src.nodata),
                'profile': src.profile.copy(),
                'identity': {'product_id': product, 'sha256': checksum},
                'path': str(path.resolve()),
            }
    return assets


def bind_looks_assets(assets, rows, vv_profiles, vh_profiles):
    """Match full native signal grids to the explicitly selected static assets."""
    if not assets:
        raise ValueError('nlooks_weighted requires loaded RTC-STATIC looks assets')
    weights, profiles, provenance = [], [], []
    for row, pv, ph in zip(rows, vv_profiles, vh_profiles):
        burst = str(row['jpl_burst_id']).upper().replace('_', '-')
        if burst not in assets:
            raise ValueError(f'Missing static looks asset for {burst}')
        if not re.fullmatch(
                rf'OPERA_L2_RTC-S1_{re.escape(burst)}_\d{{8}}T\d{{6}}Z_\d{{8}}T\d{{6}}Z_S1[AB]_30_v1\.0',
                str(row['opera_id'])):
            raise ValueError(f'nlooks_weighted requires matching RTC-S1 v1.0 product identity: {burst}')
        asset = assets[burst]
        profile = asset['profile']
        for signal in (pv, ph):
            if (rasterio.crs.CRS.from_user_input(signal['crs']) != profile['crs']
                    or signal['transform'] != profile['transform']
                    or (signal['height'], signal['width']) != asset['array'].shape):
                raise ValueError(f'Looks and VV/VH must share the full native grid: {burst}')
        weights.append(asset['array'])
        profiles.append(profile)
        provenance.append({**asset['identity'], 'path': asset['path']})
    return weights, profiles, provenance


def weighted_pair_window(indices, vv, vv_profiles, vh, vh_profiles, weights, weight_profiles,
                         height, width, transform, crs, ys, xs,
                         vv_bounds=None, vh_bounds=None):
    """Sum(w * power) / sum(w), using one positive paired support for VV/VH."""
    shape = (ys.stop - ys.start, xs.stop - xs.start)
    sums_vv = np.zeros(shape, dtype=np.float64)
    sums_vh = np.zeros(shape, dtype=np.float64)
    denominator = np.zeros(shape, dtype=np.float64)
    count = np.zeros(shape, dtype=np.uint32)
    for index in indices:
        a = _mosaic_align_scene_window(
            [index], vv, vv_profiles, height, width, transform, crs, ys, xs, vv_bounds)
        b = _mosaic_align_scene_window(
            [index], vh, vh_profiles, height, width, transform, crs, ys, xs, vh_bounds)
        w = _mosaic_align_scene_window(
            [index], weights, weight_profiles, height, width, transform, crs, ys, xs)
        if a is None or b is None or w is None:
            continue
        valid = np.isfinite(a) & np.isfinite(b) & np.isfinite(w) & (a > 0) & (b > 0) & (w > 0)
        weight = w[valid].astype(np.float64)
        sums_vv[valid] += a[valid] * weight
        sums_vh[valid] += b[valid] * weight
        denominator[valid] += weight
        count[valid] += 1
    out_vv = np.full(shape, np.nan, dtype=np.float32)
    out_vh = np.full(shape, np.nan, dtype=np.float32)
    np.divide(sums_vv, denominator, out=out_vv, where=denominator > 0)
    np.divide(sums_vh, denominator, out=out_vh, where=denominator > 0)
    return out_vv, out_vh, count, denominator
