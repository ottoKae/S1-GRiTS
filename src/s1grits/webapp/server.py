"""FastAPI application factory for the S1-GRiTS web UI (v2.3).

API-first: every capability is an HTTP endpoint (OpenAPI docs at ``/docs``);
the bundled static SPA is just one client of it. Requires the optional
``s1grits[web]`` extra (fastapi + uvicorn).

Security posture (docs/webapp_v2_architecture.md §6): default bind is
localhost; a bearer token (``--token`` / ``S1GRITS_WEB_TOKEN``) gates every
``/api`` route when set; job execution is whitelist-only with the output
directory pinned to the workspace; asset serving is traversal-safe.

NOTE: deliberately NO ``from __future__ import annotations`` here — FastAPI
resolves endpoint annotations at runtime, and stringified annotations cannot
find the function-local request models defined inside create_app.
"""
import hmac
import logging
from pathlib import Path

from s1grits.__version__ import __version__
from s1grits.webapp.catalog_api import Workspace
from s1grits.webapp.jobs import JOB_TYPES, JobManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Config the job composer pre-fills; mirrors config/s1grits_scenes.yaml's
# monthly-only production shape (kept minimal on purpose — the full annotated
# template lives in the repo and docs).
CONFIG_TEMPLATE = """\
workflow: "scenes"

roi:
  manual_mgrs_tiles:
    - "17MPU"
  flight_direction: "ASCENDING"
  polarization: "VV+VH"

time:
  full: 2026            # full archive up to this year (clipped to last complete month)
  # or: years: [2026]  +  months: [1]

output:
  base_dir: "."          # forced to the server workspace on submit
  existing_store: "resume"
  existing_month: "skip"
  formats: {cog: true, preview: true}

processing:
  target_resolution: 30.0
  tile_clip: true
  monthly:
    enabled: true
    only: true
    composite_method: "nanmedian"
    generate_cog: true
    generate_preview: true
    blockwise_threads: 2

memory:
  max_memory_gb: 'auto'
  batch_strategy: 'auto'     # demand-aware
  max_download_workers: 8
  download_prefetch: true

parallel:
  enabled: true
  max_workers: 2
"""


def create_app(root: Path | str, token: str | None = None,
               max_concurrent_jobs: int = 1,
               job_cmd_prefix: list[str] | None = None):
    """Build the FastAPI app for one workspace.

    ``job_cmd_prefix`` overrides the CLI command vector (tests inject a stub
    here; production uses the real ``s1grits`` executable).
    """
    try:
        from fastapi import FastAPI, HTTPException, Query, Request
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The web UI requires the optional 'web' extra: "
            "pip install 's1grits[web]'"
        ) from exc

    workspace = Workspace(root)
    jobs = JobManager(workspace.root, max_concurrent=max_concurrent_jobs,
                      cmd_prefix=job_cmd_prefix)

    app = FastAPI(
        title="S1-GRiTS Web API",
        version=__version__,
        description=(
            "API-first interface over an s1grits workspace: dataset browsing, "
            "visualisation probes, and a supervised job queue wrapping the CLI."
        ),
    )
    app.state.workspace = workspace
    app.state.jobs = jobs

    # -- auth ----------------------------------------------------------
    @app.middleware("http")
    async def _token_guard(request: Request, call_next):
        if token and request.url.path.startswith("/api"):
            supplied = ""
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            supplied = supplied or request.query_params.get("token", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "Invalid or missing token"},
                                    status_code=401)
        return await call_next(request)

    # -- models --------------------------------------------------------
    class JobRequest(BaseModel):
        type: str
        title: str | None = None
        config_yaml: str | None = None

    # -- workspace / datasets ------------------------------------------
    @app.get("/api/health", tags=["workspace"])
    def health():
        return {"status": "ok", "version": __version__,
                "workspace": str(workspace.root)}

    @app.get("/api/workspace", tags=["workspace"])
    def workspace_summary():
        return workspace.summary()

    @app.get("/api/tiles", tags=["datasets"])
    def tiles():
        return workspace.tiles()

    @app.get("/api/items", tags=["datasets"])
    def items(
        tile: str | None = None,
        product_type: str | None = None,
        direction: str | None = None,
        track: int | None = None,
        month_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
        month_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
        limit: int = Query(200, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ):
        return workspace.items(
            tile=tile, product_type=product_type, direction=direction,
            track=track, month_from=month_from, month_to=month_to,
            limit=limit, offset=offset,
        )

    # -- visualisation -------------------------------------------------
    @app.get("/api/timeseries", tags=["visualisation"])
    def timeseries(
        tile: str, zarr_path: str, lon: float, lat: float,
        bands: str | None = None,
    ):
        try:
            return workspace.timeseries(
                tile, zarr_path, lon, lat,
                bands=[b for b in (bands or "").split(",") if b] or None,
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, f"Store not found: {exc}")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/asset/{tile}/{relpath:path}", tags=["visualisation"])
    def asset(tile: str, relpath: str):
        try:
            path = workspace._resolve_asset(tile, relpath)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        media = {"png": "image/png", "tif": "image/tiff",
                 "json": "application/json"}.get(
            path.suffix.lstrip(".").lower(), "application/octet-stream")
        return FileResponse(path, media_type=media,
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/coverage", tags=["datasets"])
    def coverage():
        """Tiles × months coverage matrix (drives the coverage heat-strip and
        gap detection in the UI)."""
        return workspace.coverage()

    @app.get("/api/bursts", tags=["visualisation"])
    def bursts(tiles: str, direction: str | None = None):
        """Raw OPERA burst footprints (GeoJSON, EPSG:4326) overlapping the
        given comma-separated MGRS tiles — the toggleable map reference layer.
        ``direction`` filters to one orbit pass (ASCENDING/DESCENDING)."""
        tile_ids = [t.strip().upper() for t in tiles.split(",") if t.strip()]
        if not tile_ids:
            raise HTTPException(400, "tiles= requires at least one MGRS tile id")
        if direction and direction.upper() not in ("ASCENDING", "DESCENDING"):
            raise HTTPException(400, "direction must be ASCENDING or DESCENDING")
        return workspace.bursts(tile_ids, direction=direction)

    @app.get("/api/asset-bounds/{tile}/{relpath:path}", tags=["visualisation"])
    def asset_bounds(tile: str, relpath: str):
        """True WGS84 footprint of an asset (for correctly-placed overlays).

        Catalog rows carry the FULL master-grid geometry; COG/preview files
        cover only the tile-clipped crop. This endpoint returns the asset's
        actual bounds so the client does not stretch a clipped image over
        the whole grid footprint.
        """
        try:
            return workspace.asset_bounds(tile, relpath)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    # -- jobs ------------------------------------------------------------
    @app.get("/api/job-types", tags=["jobs"])
    def job_types():
        return {
            k: {"title": v["title"], "needs_config": v["needs_config"]}
            for k, v in JOB_TYPES.items()
        }

    @app.get("/api/config-template", tags=["jobs"])
    def config_template():
        return {"yaml": CONFIG_TEMPLATE}

    @app.get("/api/jobs", tags=["jobs"])
    def list_jobs():
        return jobs.list()

    @app.post("/api/jobs", tags=["jobs"], status_code=201)
    def create_job(req: JobRequest):
        try:
            job = jobs.submit(req.type, config_text=req.config_yaml,
                              title=req.title)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return job.to_dict()

    @app.get("/api/jobs/{job_id}", tags=["jobs"])
    def job_detail(job_id: str):
        try:
            return jobs.get(job_id).to_dict()
        except KeyError:
            raise HTTPException(404, f"Unknown job: {job_id}")

    @app.get("/api/jobs/{job_id}/log", tags=["jobs"])
    def job_log(job_id: str, after: int = Query(0, ge=0),
                limit: int = Query(500, ge=1, le=5000)):
        try:
            return jobs.log(job_id, after=after, limit=limit)
        except KeyError:
            raise HTTPException(404, f"Unknown job: {job_id}")

    @app.post("/api/jobs/{job_id}/cancel", tags=["jobs"])
    def job_cancel(job_id: str):
        try:
            return jobs.cancel(job_id)
        except KeyError:
            raise HTTPException(404, f"Unknown job: {job_id}")

    # -- static frontend (mounted last so /api wins) ---------------------
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True),
                  name="static")

    return app


def serve(root: str, host: str = "127.0.0.1", port: int = 8765,
          token: str | None = None, max_concurrent_jobs: int = 1,
          insecure: bool = False) -> None:
    """Run the web UI (entry point for ``s1grits serve``)."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The web UI requires the optional 'web' extra: "
            "pip install 's1grits[web]'"
        ) from exc

    if host not in ("127.0.0.1", "localhost", "::1") and not token and not insecure:
        raise SystemExit(
            "Refusing to bind a non-localhost address without --token. "
            "Set --token/-S1GRITS_WEB_TOKEN, or pass --insecure if you "
            "really want an open server."
        )
    app = create_app(root, token=token, max_concurrent_jobs=max_concurrent_jobs)
    logger.info("S1-GRiTS web UI on http://%s:%d (workspace: %s)", host, port, root)
    uvicorn.run(app, host=host, port=port, log_level="info")
