"""Select paired backscatter from the nearest full-burst valid-area centre."""
from __future__ import annotations

import numpy as np
from pyproj import CRS, Transformer

from s1grits.asf_io import NODATA_SENTINEL
from s1grits.scenes.mosaic import _mosaic_align_scene_window


def burst_centers(vv, vv_profiles, vh, vh_profiles, target_crs):
    """Compute each native paired-valid mask's centroid once, before prealignment.

    Row strips also support lazy GeoTIFF arrays without materialising a burst.
    The native centroid is transformed to the target's metric coordinates;
    neither target cropping nor processing chunks change the centre.
    """
    target = CRS.from_user_input(target_crs)
    if not target.is_projected or any(a.unit_conversion_factor != 1 for a in target.axis_info):
        raise ValueError('bursts_center requires a projected target CRS in metres')
    centers = []
    for a, pa, b, pb in zip(vv, vv_profiles, vh, vh_profiles):
        if (a.shape != b.shape or pa['transform'] != pb['transform']
                or CRS.from_user_input(pa['crs']) != CRS.from_user_input(pb['crs'])):
            raise ValueError('bursts_center requires VV/VH on the same native burst grid')
        count = 0
        row_sum = col_sum = 0.0
        h, w = a.shape
        cols = np.arange(w, dtype=np.float64) + 0.5
        for y in range(0, h, 256):
            aa, bb = np.asarray(a[y:y+256, :]), np.asarray(b[y:y+256, :])
            valid = np.isfinite(aa) & np.isfinite(bb) & (aa > 0) & (bb > 0)
            valid &= (aa != NODATA_SENTINEL) & (bb != NODATA_SENTINEL)
            for values, profile in ((aa, pa), (bb, pb)):
                if profile.get('nodata') is not None:
                    valid &= values != profile['nodata']
            count += int(valid.sum())
            row_sum += np.dot(valid.sum(axis=1), np.arange(y, y+len(aa)) + 0.5)
            col_sum += np.dot(valid.sum(axis=0), cols)
        if count == 0:
            centers.append(None)
            continue
        x, y = pa['transform'] * (col_sum/count, row_sum/count)
        center = Transformer.from_crs(pa['crs'], target, always_xy=True).transform(x, y)
        if not np.isfinite(center).all():
            raise ValueError('bursts_center cannot transform a native centroid')
        centers.append([float(center[0]), float(center[1])])
    return centers


def center_pair_window(indices, centers, vv, vv_profiles, vh, vh_profiles,
                       height, width, transform, crs, ys, xs,
                       vv_bounds=None, vh_bounds=None):
    """Same winner for VV/VH; distance ties keep the caller's stable source order.

    source_index is one-based into the acquisition's ordered OPERA asset list.
    n_candidates counts eligible sources, not contributions to an average.
    """
    shape = (ys.stop-ys.start, xs.stop-xs.start)
    yy, xx = np.ogrid[ys.start:ys.stop, xs.start:xs.stop]
    x = transform.c + transform.a*(xx+0.5) + transform.b*(yy+0.5)
    y = transform.f + transform.d*(xx+0.5) + transform.e*(yy+0.5)
    distance = np.full(shape, np.inf, dtype=np.float64)
    out_vv = np.full(shape, np.nan, dtype=np.float32)
    out_vh = np.full(shape, np.nan, dtype=np.float32)
    candidates = np.zeros(shape, dtype=np.uint32)
    source_index = np.zeros(shape, dtype=np.uint32)
    for position, index in enumerate(indices, 1):
        center = centers[index]
        if center is None:
            continue
        a = _mosaic_align_scene_window(
            [index], vv, vv_profiles, height, width, transform, crs, ys, xs, vv_bounds)
        b = _mosaic_align_scene_window(
            [index], vh, vh_profiles, height, width, transform, crs, ys, xs, vh_bounds)
        if a is None or b is None:
            continue
        valid = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
        candidates[valid] += 1
        squared_distance = (x-center[0])**2 + (y-center[1])**2
        take = valid & (squared_distance < distance)
        distance[take] = squared_distance[take]
        out_vv[take], out_vh[take] = a[take], b[take]
        source_index[take] = position
    return out_vv, out_vh, candidates, source_index
