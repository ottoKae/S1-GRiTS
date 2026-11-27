"""Local file-producing tools using the existing CLI and job queue."""
from pathlib import Path
import json
import secrets
import time

from .tool_api import local_tool_capabilities, tool_arguments


def mosaic_plan(console, body, destination):
    import rasterio
    from s1grits.analysis.mosaic import plan_grid_mosaic

    root, _ = console.resolve_catalog(body['root_id'], body['output'])
    _, frame, _ = console._valid_catalog(body['output'], body['root_id'])
    roots = [entry['path'] for entry in console._catalog_roots.values()]
    for item in body['item_ids']:
        selected = frame[frame.item_id == item]
        if len(selected) != 1:
            raise ValueError('Select distinct, complete Catalog item IDs')
        row = selected.iloc[0]
        if not isinstance(row.get('cog_path'), str) or not row.cog_path:
            raise ValueError(f'No COG asset for {item}')
        asset = root/row.cog_path
        if not asset.exists():
            asset = root/row.tile_id/row.cog_path
        asset = asset.resolve()
        if not any(asset.is_relative_to(allowed) for allowed in roots):
            raise PermissionError('COG asset is outside registered data roots')
        # The Web input is a GeoTIFF, not a VRT referencing unregistered files.
        try:
            with rasterio.open(asset, driver='GTiff'):
                pass
        except rasterio.errors.RasterioIOError as exc:
            raise ValueError(f'Input must be a readable GeoTIFF: {asset}') from exc
    name = 'mosaic.vrt' if body['output_format'] == 'VRT' else 'mosaic.tif'
    return plan_grid_mosaic(root, body['item_ids'], destination/name,
        target_crs=body['target_crs'], resolution=body['resolution'], bounds=body['bounds'],
        output_format=body['output_format'])


def create_file_plan(console, request):
    from .cn_api import _json_safe

    body = request.model_dump(mode='json')
    root, _ = console.resolve_catalog(body['root_id'], body['output'])
    destination = console._tool_destination(body['output_subdir'], root, [])
    if destination.is_relative_to(root):
        raise ValueError('Write analysis and mosaics outside the source Cube root')
    capabilities = local_tool_capabilities()
    operation = body['operation']
    warnings = ['UTC timestamps are inclusive; an end date alone means midnight.',
                'Outputs cannot resume in place. Retain partial results and select a new directory.']
    extra, raw_gib = {}, 0
    if operation == 'mosaic_grid':
        if not capabilities['mosaic_grid']['available']:
            raise ValueError('Grid mosaics require GDAL with VRT and COG drivers')
        definition = mosaic_plan(console, body, destination)
        parents = [dict(record=record) for record in definition['inputs']]
        grid = definition['contract']
        size = grid['shape'][0]*grid['shape'][1]*len(grid['bands'])*4/1024**3
        raw_gib = size if body['output_format'] == 'COG' else 0
        extra.update(mosaic=definition, materialized_gib=size, result_type='raster',
                     receipt_name=Path(definition['output']).name+'.json')
        warnings += ['Input order defines per-band first-valid priority.',
                     'Keep original COGs and the .sources directory for VRT access.']
    else:
        if not capabilities['landtrendr']['available']:
            raise ValueError('Local analysis requires separately installed landtrendr==0.4.0')
        from s1grits.analysis.segmentation import _read_point, _annual_series
        from s1grits.analysis.segmentation_batch import _select_points

        detail = console.catalog_product(body['output'], body['item_id'], body['root_id'])
        parents = [detail]
        selection = ([dict(point_id='pixel', col=body['pixel'][0], row=body['pixel'][1])]
                     if operation == 'analyze_segments' else _select_points(body['points'], body['window']))
        samples = []
        for point in selection:
            frame, _, lineage, _ = _read_point(root, body['item_id'], destination, body['band'],
                [point['col'], point['row']], body['start'], body['end'])
            annual = _annual_series(frame, body['start'], body['end'], body['annual_method'],
                                    body['annual_domain'], body['min_annual_observations'])
            count = int(annual.value_db.notna().sum())
            if count > 128:
                raise ValueError('Local LandTrendr supports at most 128 valid annual observations')
            samples.append(dict(**point, selected_observations=len(frame),
                valid_observations=int(frame.value_db.notna().sum()), model_observations=count,
                input=lineage))
        extra.update(samples=samples, result_type='analysis',
                     receipt_name='analysis.json' if operation == 'analyze_segments' else 'batch.json')
        warnings += detail['warnings'] + ['Local LandTrendr is experimental (nthh/LT-rs 0.4.0).',
            'Fewer than six valid years produces insufficient_observations, not zero change events.',
            'Batch failures retain per-point status and do not count as zero segments.']
    if raw_gib > 500:
        raise ValueError('Estimated output exceeds the 500 GiB service limit; select smaller bounds')
    plan = dict(operation=operation, request=body, parents=parents, catalog_root=str(root),
                output_dir=str(destination), output_subdir=body['output_subdir'], raw_gib=raw_gib,
                command=tool_arguments(body, root, destination), warnings=warnings,
                confirmation_required=raw_gib >= 10,
                confirmation_phrase=f'mosaic {raw_gib:.2f} GiB' if raw_gib >= 10 else '',
                expires=time.time()+3600, consumed=False, **extra)
    plan = _json_safe(plan)
    token = secrets.token_urlsafe(32)
    with console._lock:
        console._plans[token] = plan
    return dict(plan, plan_id=token)


def file_result(jobs, job_id):
    """Read fixed result files only; small batch and fitted tables are bounded."""
    import pandas as pd
    from .cn_api import _json_safe
    from .tool_api import FILE_TOOLS

    job = jobs.get(job_id)
    if job.type not in FILE_TOOLS:
        raise ValueError('This task does not produce analysis or raster files')
    root = Path(job.metadata['output_dir'])
    receipt_path = Path(job.metadata['receipt'])
    receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.is_file() else None
    tables = {}
    names = ('point_summary',) if job.type == 'analyze_segments_batch' else ('segments', 'annual_fitted')
    if job.type != 'mosaic_grid':
        for name in names:
            path = root/(name+'.parquet')
            if path.is_file():
                frame = pd.read_parquet(path)
                tables[name] = dict(columns=list(frame.columns), total=len(frame),
                                    rows=frame.head(128).to_dict(orient='records'))
    return _json_safe(dict(job_status=job.status, result_type=job.metadata['result_type'],
        output_dir=str(root), receipt_path=str(receipt_path), receipt=receipt, tables=tables))
