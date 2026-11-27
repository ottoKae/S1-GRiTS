"""Finite Web requests and argument vectors for the existing tool CLIs."""
from typing import Literal
from importlib.util import find_spec
from importlib.metadata import version, PackageNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeriveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["derive_cube"] = "derive_cube"
    root_id: str = "workspace"
    output: str = ""
    item_id: str
    output_subdir: str
    start: str
    end: str
    features: list[Literal["ratio", "rvi", "glcm"]] = Field(default_factory=lambda: ["ratio", "rvi"])
    spatial_filter: Literal["none", "tv_bregman", "nlm"] = "none"
    monthly: Literal["mean", "median"] | None = None
    interpolation: Literal["none", "linear"] | None = None
    interpolation_domain: Literal["db", "linear_power"] | None = None
    max_gap_days: float | None = Field(None, gt=0, allow_inf_nan=False)
    window: tuple[int, int, int, int] | None = None
    chunk_size: int = Field(256, ge=1, le=1024)

    @model_validator(mode="after")
    def temporal_options(self):
        if self.interpolation is not None:
            if self.features or self.spatial_filter != "none" or self.monthly:
                raise ValueError("Run interpolation separately: no features, filter or monthly composite")
            if self.interpolation_domain is None:
                raise ValueError("Interpolation requires an explicit db or linear_power domain")
            if (self.interpolation == "linear") != (self.max_gap_days is not None):
                raise ValueError("Only linear interpolation requires max_gap_days")
        elif self.interpolation_domain is not None or self.max_gap_days is not None:
            raise ValueError("Select an interpolation method before domain or gap")
        self.features = [name for name in ("ratio", "rvi", "glcm") if name in self.features]
        return self


class ViewDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_id: str = "workspace"
    output: str = ""
    output_subdir: str


class ViewCreateRequest(ViewDestination):
    operation: Literal["view_create"]
    item_id: str
    start: str
    end: str
    window: tuple[int, int, int, int] | None = None
    bands: list[Literal["VV_dB", "VH_dB", "Ratio", "RVI"]] = Field(
        default_factory=lambda: ["VV_dB", "VH_dB", "Ratio", "RVI"], min_length=1)


class ViewMaterializeRequest(ViewDestination):
    operation: Literal["view_materialize"]
    item_id: str
    chunk_size: int = Field(256, ge=1, le=1024)


class ViewMultitrackRequest(ViewDestination):
    operation: Literal["view_multitrack"]
    item_ids: list[str] = Field(min_length=2)
    start_month: str
    end_month: str
    method: Literal["first_valid_pixel", "mean"]
    window: tuple[int, int, int, int] | None = None


class AnalysisPoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    point_id: str = Field(min_length=1)
    col: int = Field(ge=0, strict=True)
    row: int = Field(ge=0, strict=True)


class SegmentsRequest(ViewDestination):
    operation: Literal['analyze_segments', 'analyze_segments_batch']
    item_id: str
    band: Literal['VV_dB', 'VH_dB']
    start: str
    end: str
    annual_method: Literal['mean', 'median']
    annual_domain: Literal['db', 'linear_power']
    disturbance_direction: Literal['increase', 'decrease']
    min_annual_observations: int = Field(1, ge=1)
    pixel: tuple[int, int] | None = None
    window: tuple[int, int, int, int] | None = None
    points: list[AnalysisPoint] | None = Field(None, min_length=1, max_length=64)

    @model_validator(mode='after')
    def selection(self):
        if self.operation == 'analyze_segments':
            if self.pixel is None or self.window is not None or self.points is not None:
                raise ValueError('Single-point analysis requires pixel only')
        elif self.pixel is not None or (self.window is None) == (self.points is None):
            raise ValueError('Batch analysis requires exactly one of points or window')
        if self.window is not None and self.window[2]*self.window[3] > 64:
            raise ValueError('Web analysis supports at most 64 pixels per task')
        return self


class MosaicRequest(ViewDestination):
    operation: Literal['mosaic_grid']
    item_ids: list[str] = Field(min_length=1)
    target_crs: str
    resolution: float = Field(gt=0, allow_inf_nan=False)
    bounds: tuple[float, float, float, float]
    output_format: Literal['VRT', 'COG']


TOOL_OPERATIONS = ("derive_cube", "view_create", "view_materialize", "view_multitrack",
                   'analyze_segments', 'analyze_segments_batch', 'mosaic_grid')
FILE_TOOLS = ('analyze_segments', 'analyze_segments_batch', 'mosaic_grid')


def local_tool_capabilities():
    lt = dict(available=False, status='experimental', implementation='nthh/LT-rs', required_version='0.4.0')
    try:
        lt['version'] = version('landtrendr')
        if lt['version'] != '0.4.0':
            raise ImportError('Requires separately installed landtrendr==0.4.0')
        import landtrendr
        lt['available'] = callable(landtrendr.pixel)
    except (ImportError, OSError, AttributeError, PackageNotFoundError) as exc:
        lt['reason'] = str(exc)
    mosaic = dict(available=False)
    try:
        from osgeo import gdal
        mosaic.update(available=bool(gdal.GetDriverByName('VRT') and gdal.GetDriverByName('COG')),
                      version=gdal.VersionInfo('--version'))
    except (ImportError, OSError) as exc:
        mosaic['reason'] = str(exc)
    return dict(landtrendr=lt, mosaic_grid=mosaic, max_analysis_points=64,
                cusum=dict(available=False, status='distribution_review_pending'),
                ccdc=dict(available=False, status='local_not_implemented'),
                earthengine=dict(available=False, status='not_exposed_in_web'))


def views_available():
    return find_spec('dask') is not None


def tool_arguments(request, catalog_root, destination, job_dir=None):
    operation = request['operation']
    if operation == 'derive_cube':
        return derive_arguments(request, catalog_root, destination)
    if operation not in TOOL_OPERATIONS:
        raise ValueError('Unknown tool operation')
    if operation in FILE_TOOLS:
        from pathlib import Path

        control = Path(job_dir) if job_dir is not None else Path('<job-directory>')
        args = [operation, '--catalog-root', str(catalog_root), '--output', str(destination)]
        if operation == 'mosaic_grid':
            args[-1] = str(Path(destination)/('mosaic.vrt' if request['output_format']=='VRT' else 'mosaic.tif'))
            return args + ['--items-file', str(control/'items.json'), '--crs', request['target_crs'],
                           '--resolution', str(request['resolution']), '--bounds', *map(str, request['bounds']),
                           '--format', request['output_format']]
        args.extend(['--item-id', request['item_id']])
        for key in ('band','start','end','annual_method','annual_domain','disturbance_direction','min_annual_observations'):
            args.extend(['--'+key.replace('_','-'), str(request[key])])
        if operation == 'analyze_segments':
            args.extend(['--backend','local','--algorithm','landtrendr','--pixel', *map(str,request['pixel'])])
        elif request['points'] is not None:
            args.extend(['--points-file',str(control/'points.json')])
        else:
            args.extend(['--window',*map(str,request['window'])])
        return args
    args = ['view', operation.removeprefix('view_'), '--catalog-root', str(catalog_root),
            '--output', str(destination)]
    if operation == 'view_multitrack':
        args.extend(['--item-ids', *request['item_ids'], '--start-month', request['start_month'],
                     '--end-month', request['end_month'], '--method', request['method']])
    else:
        args.extend(['--item-id', request['item_id']])
        if operation == 'view_create':
            args.extend(['--start', request['start'], '--end', request['end'], '--bands', *request['bands']])
        else:
            args.extend(['--chunk-size', str(request['chunk_size'])])
    if request.get('window') is not None:
        args.extend(['--window', *map(str, request['window'])])
    return args


def derive_arguments(request, catalog_root, destination):
    args = ["derive_cube", "--catalog-root", str(catalog_root), "--item-id", request["item_id"],
            "--output", str(destination), "--start", request["start"], "--end", request["end"],
            "--features", *request["features"], "--filter", request["spatial_filter"],
            "--chunk-size", str(request["chunk_size"])]
    for key in ("monthly", "interpolation", "interpolation_domain", "max_gap_days"):
        if request[key] is not None:
            args.extend(["--" + key.replace("_", "-"), str(request[key])])
    if request["window"] is not None:
        args.extend(["--window", *map(str, request["window"])])
    return args
