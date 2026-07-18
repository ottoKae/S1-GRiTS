# S1-GRiTS Web UI v2.3 — Reference

The v2.3 web interface (`s1grits serve`) is an API-first dashboard over a
processing workspace: dataset browsing/filtering, interactive visualisation
(map, monthly timeline, per-pixel time-series probes), and a download manager
that supervises the existing CLI. Strategy and architecture rationale:
[`webapp_v2_architecture.md`](webapp_v2_architecture.md).

## 1. Install & run

```bash
pip install "s1grits[web]"

# Local use (default bind 127.0.0.1)
s1grits serve --root /path/to/outputs --port 8765

# Remote box (HPC login node): keep it local + SSH-forward
ssh -L 8765:127.0.0.1:8765 user@server
# ...or bind publicly WITH a token:
S1GRITS_WEB_TOKEN=$(openssl rand -hex 16) s1grits serve --root outputs --host 0.0.0.0
```

| `serve` flag | Default | Meaning |
|---|---|---|
| `--root` | (required) | Workspace = the `output.base_dir` of your runs |
| `--host` / `--port` | `127.0.0.1` / `8765` | Non-localhost binds require `--token` (or `--insecure`) |
| `--token` | `$S1GRITS_WEB_TOKEN` | Bearer token required on every `/api` route |
| `--max-concurrent-jobs` | `1` | Parallel pipeline jobs (runs parallelise internally) |

Open `http://127.0.0.1:8765/`. With a token, append `?token=...` once — the
SPA stores it. Interactive OpenAPI docs: `http://127.0.0.1:8765/docs`.

## 2. HTTP API

All routes are JSON unless noted. When a token is configured, send
`Authorization: Bearer <token>` (or `?token=`). The generated OpenAPI schema
at `/docs` / `/openapi.json` is authoritative; this table is the tour.

### Workspace & datasets

| Route | Description |
|---|---|
| `GET /api/health` | `{status, version, workspace}` — liveness + version |
| `GET /api/workspace` | Tile/item counts, disk free/total |
| `GET /api/tiles` | Per-tile summary: WGS84 bounds, products, directions, tracks, month range, processing-report flags |
| `GET /api/items` | Filtered, paginated catalog records. Query: `tile, product_type, direction, track, month_from, month_to (YYYY-MM), limit (≤2000), offset`. Returns `{total, items[], months{}}` where `months` is the per-month histogram of the **filtered** set (drives the timeline). Each item carries `bounds4326`, `zarr_path`, `cog_path`, `preview_path`. |

### Visualisation

| Route | Description |
|---|---|
| `GET /api/timeseries?tile=&zarr_path=&lon=&lat=[&bands=a,b]` | Per-pixel time series from one Zarr store at a WGS84 point. Reads `O(n_time)` values (never a spatial slab). Defaults to VV/VH/Ratio/RVI where present and always appends `n_obs` when the store has it. `400` outside the grid, `403` on traversal, `404` missing store. |
| `GET /api/asset/{tile}/{relpath}` | Serves catalog-relative files (preview PNG, COG, STAC JSON) with `Cache-Control`. Traversal-safe: resolved paths must stay inside the tile dir; hidden paths (incl. `.webapp/`) are refused. |

### Jobs (download manager)

| Route | Description |
|---|---|
| `GET /api/job-types` | Whitelisted types: `process_scenes`, `process`, `catalog_resync`, `doctor` |
| `GET /api/config-template` | Starter YAML for the composer |
| `POST /api/jobs` | Body `{type, title?, config_yaml?}` → `201` + job record. The YAML is safe-loaded and rewritten with `output.base_dir` **pinned to the workspace**; invalid YAML/type → `400`. |
| `GET /api/jobs` | All jobs, newest first, with `{status, progress:{per_tile:{TILE:[batch,total]}, pct}, log_lines}` |
| `GET /api/jobs/{id}` | One job record |
| `GET /api/jobs/{id}/log?after=N&limit=M` | Incremental log: lines `≥ after`, plus `next` cursor — poll with `after=next` for tail -f semantics. Credential-looking values are `[REDACTED]`. |
| `POST /api/jobs/{id}/cancel` | SIGTERM → SIGKILL after grace; queued jobs cancel instantly |

Job state and logs live in `{root}/.webapp/jobs/{id}/` (`job.json`,
`config.yaml`, `log.txt`) and survive server restarts; a job that was running
when the server died is marked `failed` with an explanatory error.

## 3. Frontend components (`src/s1grits/webapp/static/`)

Zero-build vanilla ES modules; Leaflet 1.9 is the only external library
(CDN, SRI-pinned). `app.js` is organised into sections that map 1:1 to UI
regions:

| Section (`app.js`) | DOM region | Behaviour |
|---|---|---|
| `api` | — | `fetch` wrapper: bearer token from `?token=`/localStorage, JSON errors surfaced |
| `workspace` | top bar chips | `/api/health` + `/api/workspace` summary |
| `tiles / map` | sidebar list + Leaflet | Tile footprints (WGS84 bounds) on a dark basemap; click toggles the tile filter; ⚠ marks tiles with processing-report findings |
| `filters` | sidebar | tile/product/direction/track/month-range selects, options derived from `/api/tiles` |
| `items table` | bottom table | Paginated `/api/items`; row click opens the detail panel; asset links (png/cog) |
| `timeline` | canvas strip | Continuous month axis (gaps visible) from `items.months`; click filters one month, shift-click extends the range, re-click clears |
| `detail panel` | right column | Metadata, preview image, COG download; selecting an item overlays its preview PNG on the map at its true bounds |
| `probe` | right column charts | With an item selected, clicking inside its footprint calls `/api/timeseries`; multi-band canvas chart + `n_obs` confidence strip underneath |
| `jobs drawer` | right drawer | 2.5 s poll of `/api/jobs`: status pills, per-tile batch progress bars, cancel, incremental log viewer; “New job” modal pre-fills the config template |

Responsive: three-column grid ≥1200 px, two-column to 768 px, single column
below (drawer becomes full-width overlay).

## 4. Headless / automated operation

Everything the UI does is one endpoint away — CI and cron never need a
browser. The CLI remains fully supported for direct use; the API adds a
remote-controllable variant of the same whitelisted commands.

```bash
BASE=http://127.0.0.1:8765; AUTH="Authorization: Bearer $S1GRITS_WEB_TOKEN"

# What do I have?
curl -sH "$AUTH" $BASE/api/tiles | jq '.[].tile_id'
curl -sH "$AUTH" "$BASE/api/items?product_type=smonthly&month_from=2026-01" | jq .total

# Queue a processing run (identical semantics to `s1grits process_scenes --config cfg.yaml`)
JOB=$(curl -sH "$AUTH" -X POST $BASE/api/jobs \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"process_scenes\",\"title\":\"nightly\",\"config_yaml\":$(jq -Rs . < cfg.yaml)}" | jq -r .id)

# Follow it
watch -n5 "curl -sH '$AUTH' $BASE/api/jobs/$JOB | jq '{status,progress}'"
curl -sH "$AUTH" "$BASE/api/jobs/$JOB/log?after=0" | jq -r '.lines[]'

# Probe a pixel time series (e.g. for a QA script)
curl -sH "$AUTH" "$BASE/api/timeseries?tile=17MPU&zarr_path=smonthly_ASCENDING/zarr/s1grits_smonthly_17MPU_ASCENDING_TK18.zarr&lon=-79.6&lat=-2.1" | jq .bands.VV_dB
```

## 5. Relationship to the v1.0 GUI

This web interface replaces the v1.0 Streamlit GUI (`s1grits-gui`), which was
removed in v3.0.0. Feature mapping: Mapping/Tile tabs → map + detail + probe;
Catalog tab → workspace summary + `catalog_resync` job; Process tab → job
composer; the tkinter folder picker is replaced by `--root` at server start
(headless-safe). The v1.0 runner's security properties (list-form subprocess,
log redaction) are preserved and extended (whitelist, output-dir pinning,
token auth).

## 6. Testing

`tests/test_webapp_api.py` builds a synthetic one-tile workspace (catalog +
Zarr with `n_obs` + preview PNG) and exercises: dataset queries and WGS84
bounds, month histograms and pagination, time-series correctness at a known
pixel, asset traversal protection, token gating, and the full job lifecycle
(progress parsing, redaction, config pinning, failure, cancel, restart
persistence) against a stub CLI. Run: `pytest tests/test_webapp_api.py`.
