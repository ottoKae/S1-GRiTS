"""Small native-grid point sets and windows using the existing local point fitter."""
from __future__ import annotations

import hashlib

import pandas as pd

from s1grits.atomic_write import atomic_write_json, atomic_write_parquet
from s1grits.product_instance import make_processing_signature
from .segmentation import _read_parent, analyze_segments


def analyze_segments_batch(catalog_root, item_id, output_root, *, points=None, window=None,
                           band, start, end, annual_method, annual_domain,
                           disturbance_direction, min_annual_observations=1):
    """Run local LandTrendr for explicit point_id/col/row records or a native window.

    Each point has an independent result and complete time series. Failed points
    are recorded and later points continue; the batch status is partial or failed.
    """
    selection = _select_points(points, window)
    _, parent, _, destination = _read_parent(catalog_root, item_id, output_root)
    options = dict(algorithm='landtrendr', backend='local', band=band, start=start, end=end,
                   annual_method=annual_method, annual_domain=annual_domain,
                   disturbance_direction=disturbance_direction,
                   min_annual_observations=min_annual_observations)
    parent_identity = {key: parent.get(key) for key in (
        'item_id', 'product_type', 'processing_signature', 'grid_id', 'geometry_group_id', 'track')}
    contract = dict(contract_version='local_lt_batch_v2', parent_identity=parent_identity,
                    selection=selection, options=options, execution='sequential_full_series_per_pixel')
    receipt = dict(status='building', contract=contract, parent_reference_item=parent, points=[],
                   started_utc=pd.Timestamp.now(tz='UTC').isoformat())
    destination.mkdir(parents=True)
    atomic_write_json(receipt, destination/'batch.json')
    # shortcut: one catalog/store open per point; optimize only when small samples cease to suffice.
    for point in selection:
        relative = 'points/'+hashlib.sha256(point['point_id'].encode('utf-8')).hexdigest()
        row = dict(**point, result_path=relative, status='failed', analysis_state=None,
                   processing_signature=None, selected_observations=None, valid_observations=None,
                   model_observations=None, segments=None, sample_x=None, sample_y=None,
                   error_type=None, error=None)
        try:
            result = analyze_segments(catalog_root, item_id, destination/relative,
                                      pixel=[point['col'], point['row']], **options)
            row.update({key: result[key] for key in ('status', 'analysis_state', 'processing_signature',
                       'selected_observations', 'valid_observations', 'model_observations')})
            row['sample_x'], row['sample_y'] = result['contract']['input']['pixel_center']
            if result['analysis_state'] == 'fitted':
                row['segments'] = result['segments']
        except Exception as exc:
            row.update(error_type=type(exc).__name__, error=str(exc))
        receipt['points'].append(row)
        atomic_write_json(receipt, destination/'batch.json')
    complete = sum(row['status'] == 'complete' for row in receipt['points'])
    receipt.update(status='complete' if complete == len(selection) else ('partial' if complete else 'failed'),
                   completed_points=complete, failed_points=len(selection)-complete,
                   completed_utc=pd.Timestamp.now(tz='UTC').isoformat())
    receipt['processing_signature'] = make_processing_signature(dict(contract=contract,
        point_signatures=[row['processing_signature'] for row in receipt['points']]))
    summary = pd.DataFrame(receipt['points'])
    for column in ('selected_observations', 'valid_observations', 'model_observations', 'segments'):
        summary[column] = summary[column].astype('Int64')
    atomic_write_parquet(summary, destination/'point_summary.parquet')
    atomic_write_json(receipt, destination/'batch.json')
    return receipt


def _select_points(points, window):
    if (points is None) == (window is None):
        raise ValueError('Select exactly one of points or window')
    if window is not None:
        if (len(window) != 4 or any(type(v) is not int for v in window)
                or min(window[:2]) < 0 or min(window[2:]) < 1):
            raise ValueError('window must be integer XOFF YOFF WIDTH HEIGHT with positive size')
        x, y, width, height = window
        return [dict(point_id=f'r{row}_c{col}', col=col, row=row)
                for row in range(y, y+height) for col in range(x, x+width)]
    if not isinstance(points, list) or not points:
        raise ValueError('points must be a nonempty list of point_id/col/row records')
    seen, selected = set(), []
    for point in points:
        if (not isinstance(point, dict) or set(point) != {'point_id', 'col', 'row'}
                or not isinstance(point['point_id'], str) or not point['point_id'].strip()
                or any(type(point[key]) is not int for key in ('col', 'row'))):
            raise ValueError('Each point needs a nonempty string point_id and integer col/row')
        if point['point_id'] in seen:
            raise ValueError('point_id must be unique')
        seen.add(point['point_id'])
        selected.append(dict(point))
    return selected
