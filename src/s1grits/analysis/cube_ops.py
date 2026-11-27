"""Selection and pointwise operations shared by derivatives and local views."""
import numpy as np
import pandas as pd
from rasterio.transform import Affine

from s1grits.asf_io import NODATA_SENTINEL


def select_parent(dataset, attrs, start, end, window):
    for band in ('VV_dB', 'VH_dB'):
        if band not in dataset or set(dataset[band].dims) != {'time', 'y', 'x'}:
            raise ValueError('Parent must expose VV_dB/VH_dB with named time/y/x dimensions')
    times = pd.to_datetime(dataset.time.values, utc=True, errors='coerce')
    if times.isna().any() or times.has_duplicates or not times.is_monotonic_increasing:
        raise ValueError('Parent timestamps must be valid, unique and increasing')
    indices = np.flatnonzero((times >= start) & (times <= end)).tolist()
    if not indices:
        raise ValueError('No parent observations in the selected interval')
    height, width = dataset.sizes['y'], dataset.sizes['x']
    if window is None:
        window = [0, 0, width, height]
    if (len(window) != 4 or any(not isinstance(v, int) for v in window)
            or min(window[:2]) < 0 or min(window[2:]) < 1
            or window[0]+window[2] > width or window[1]+window[3] > height):
        raise ValueError('window must be integer x/y offset and positive width/height inside the parent')
    native = Affine(*attrs['transform'][:6])
    x, y = dataset.x.values, dataset.y.values
    if (native.b != 0 or native.d != 0 or native.a <= 0 or native.e >= 0
            or not np.allclose(x, native.c+(np.arange(len(x))+.5)*native.a, rtol=0, atol=1e-7)
            or not np.allclose(y, native.f+(np.arange(len(y))+.5)*native.e, rtol=0, atol=1e-7)):
        raise ValueError('Parent coordinates must match its north-up affine grid')
    return times, indices, list(window), native


def parent_masks(dataset):
    names = [f'{prefix}_{kind}_mask' for prefix in ('VV', 'VH') for kind in ('observed', 'interpolated')]
    masks = {name: dataset[name] for name in names if name in dataset}
    for prefix in ('VV', 'VH'):
        if f'{prefix}_interpolated_mask' in masks and f'{prefix}_observed_mask' not in masks:
            raise ValueError('Interpolated parent requires its observed mask')
    if any(set(mask.dims) != {'time', 'y', 'x'} for mask in masks.values()):
        raise ValueError('Parent masks require named time/y/x dimensions')
    return masks


def mask_values(array):
    if not np.isin(array, [0, 1]).all():
        raise ValueError('Parent masks must contain only 0 or 1')
    return array.astype('u1', copy=False)


def clean_backscatter(array, fill=None):
    value = np.array(array, dtype='f4', copy=True)
    invalid = ~np.isfinite(value) | (value == NODATA_SENTINEL)
    if fill is not None and np.isfinite(fill):
        invalid |= value == fill
    value[invalid] = np.nan
    return value


def dual_pol_features(a, b, features):
    values = {}
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        av, bv = np.power(np.float32(10), a/10), np.power(np.float32(10), b/10)
        valid = np.isfinite(av) & np.isfinite(bv) & (av > 0) & (bv > 0)
        if 'ratio' in features:
            values['Ratio'] = np.where(valid, bv/av, np.nan)
        if 'rvi' in features:
            values['RVI'] = np.where(valid, 4*bv/(av+bv), np.nan)
    return values
