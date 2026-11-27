"""Retrospective gap filling on the parent's existing timestamps."""
from __future__ import annotations

import numpy as np

from s1grits.scenes.blocks import _iter_spatial_blocks


def _write_interpolated_store(output, read_db, times, indices, window, chunk, options):
    x0, y0, width, height = window
    stamps = times[indices].tz_localize(None).to_numpy(dtype='datetime64[ns]')
    # Keep time empty until all spatial blocks are written; incomplete stores are not catalogued.
    for prefix in ('VV', 'VH'):
        for suffix in ('_dB', '_observed_mask', '_interpolated_mask'):
            output[prefix+suffix].resize((len(stamps), height, width))
    for ys, xs in _iter_spatial_blocks(height, width, chunk, chunk):
        values = read_db(indices, slice(y0+ys.start, y0+ys.stop), slice(x0+xs.start, x0+xs.stop))
        for prefix, db in zip(('VV', 'VH'), values):
            filled, observed, interpolated = _interpolate_block(db, stamps.astype('int64'), options)
            output[prefix+'_dB'][:, ys, xs] = filled
            output[prefix+'_observed_mask'][:, ys, xs] = observed
            output[prefix+'_interpolated_mask'][:, ys, xs] = interpolated
    output['time'].resize((len(stamps),))
    output['time'][:] = stamps


def _interpolate_block(db, times_ns, options):
    """Inputs are sanitized float32 dB and strictly increasing UTC nanoseconds."""
    observed = np.isfinite(db)
    interpolated = np.zeros(db.shape, dtype='uint8')
    result = db.copy()
    if options['method'] == 'none':
        return result, observed.astype('uint8'), interpolated
    values = db.reshape(len(db), -1).astype('f8')
    if options['domain'] == 'linear_power':
        with np.errstate(over='ignore', under='ignore', invalid='ignore'):
            values = np.power(10.0, values/10)
    anchors_valid = np.isfinite(values)
    if options['domain'] == 'linear_power':
        anchors_valid &= values > 0
    seconds = (times_ns-times_ns[0])/1e9
    output = result.reshape(len(db), -1)
    flags = interpolated.reshape(len(db), -1)
    missing = ~observed.reshape(len(db), -1)
    for pixel in np.flatnonzero(missing.any(axis=0)):
        anchors = np.flatnonzero(anchors_valid[:, pixel])
        if len(anchors) < 2:
            continue
        targets = np.flatnonzero(missing[:, pixel])
        targets = targets[(targets > anchors[0]) & (targets < anchors[-1])]
        right = np.searchsorted(anchors, targets)
        span = times_ns[anchors[right]]-times_ns[anchors[right-1]]
        targets = targets[span/86400e9 <= options['max_gap_days']]
        estimates = np.interp(seconds[targets], seconds[anchors], values[anchors, pixel])
        if options['domain'] == 'linear_power':
            estimates = 10*np.log10(estimates)
        output[targets, pixel] = estimates.astype('f4')
        flags[targets, pixel] = 1
    return result, observed.astype('uint8'), interpolated
