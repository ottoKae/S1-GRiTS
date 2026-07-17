# S1-GRiTS Web UI v2.3 — Optimisation Strategy & Architecture

This document is deliverable 1 of the GUI overhaul: the complete optimisation
strategy behind the v2.3 web interface, grounded in a code-level audit of the
v1.0 GUI (`src/gui/`, ~4,900 lines of Streamlit). Deliverable 2 — the
implementation — lives in `src/s1grits/webapp/` and is documented in
[`docs/webapp.md`](webapp.md).

---

## 1. Audit of the v1.0 GUI

v1.0 is a five-tab Streamlit app (Process / Mapping / Catalog / Tile / Mosaic)
with a subprocess runner (`gui/runner.py`) and a YAML config builder. It works
on a workstation with a display, but it has structural limits that no amount
of in-place patching removes:

| # | v1.0 limitation | Root cause | Consequence |
|---|---|---|---|
| 1 | **Cannot run headless.** Folder selection uses `tkinter.filedialog` | Desktop-widget dependency inside a "web" app | Unusable on the HPC nodes where the data actually lives |
| 2 | **One run at a time, no queue.** `CommandRunner` raises if a process is running | UI thread owns the process | No download queue, no batching of tile jobs, closing the tab orphans the run |
| 3 | **Whole-page rerender per interaction.** Streamlit re-executes the script top-to-bottom on every widget event | Framework model | Map/chart state resets, log viewer stutters, poll loop (`time.sleep(0.4)`) fights the rerun loop |
| 4 | **State lives in the browser session.** `st.session_state` only | No server-side job store | Refresh = lose the running job's progress view; two users cannot see the same queue |
| 5 | **Heavy, brittle styling.** 979 lines of CSS injected as HTML strings | Fighting the framework's DOM | Every Streamlit upgrade risks visual breakage |
| 6 | **Data access re-reads everything.** Each tab loads `catalog.parquet` and Zarr stores ad hoc | No shared data layer | Repeated multi-second loads; no pagination for large catalogs |
| 7 | **No API.** Functionality is trapped inside the UI process | UI == app | Nothing scriptable; the CLI and GUI duplicate logic instead of sharing it |

The one thing v1.0 got right — and v2.3 keeps as a load-bearing principle —
is that **the processing pipeline is only ever driven through the audited CLI
via list-form `subprocess` (never `shell=True`)**, with log sanitisation of
credentials. v2.3 generalises this into a job manager rather than replacing
it with in-process execution.

---

## 2. Architecture: API-first, files-as-truth

```
                    ┌─────────────────────────────────────────────┐
                    │                Browser (SPA)                 │
                    │  Leaflet map · filters · timeline · charts   │
                    │  download manager (polls /api/jobs)          │
                    └──────────────────┬──────────────────────────┘
                                       │ HTTP/JSON (+ static assets)
                    ┌──────────────────▼──────────────────────────┐
                    │        FastAPI server  (s1grits serve)       │
                    │                                              │
                    │  catalog_api.Workspace   jobs.JobManager     │
                    │  • tile/item queries     • bounded queue     │
                    │  • WGS84 bounds cache    • subprocess CLI    │
                    │  • Zarr time-series      • progress parsing  │
                    │  • safe asset serving    • persistent logs   │
                    └──────────┬──────────────────────┬───────────┘
                               │ read-only            │ spawn (whitelist)
                    ┌──────────▼──────────┐  ┌────────▼───────────┐
                    │  Workspace on disk  │  │  s1grits CLI       │
                    │  {TILE}/catalog.par │  │  process_scenes    │
                    │  zarr / cog / png   │  │  catalog resync …  │
                    └─────────────────────┘  └────────────────────┘
```

Three decisions define the system; everything else follows from them.

### 2.1 The filesystem workspace stays the single source of truth

The pipeline already produces a well-defined on-disk contract: per-tile
`catalog.parquet`, per-track Zarr cubes, COGs, previews, STAC items,
`processing_report.json`. v2.3 adds **no database**. The server is a stateless
view over the workspace plus a small job ledger (`.webapp/jobs/`). Rationale:

- The CLI, the web UI, and any user's own notebooks all see the same state
  with zero synchronisation machinery — the exact property that lets the GUI
  and headless operations coexist.
- Crash recovery is free: the workspace's `existing_store: resume` semantics
  already make re-running safe; the UI simply reflects whatever is on disk.
- Satellite archives are large and cold; duplicating their metadata into a
  DB creates a second thing to resync (v1.0's Catalog tab existed largely to
  fix that class of drift).

### 2.2 Jobs are the CLI, supervised — never a second pipeline

The download manager does not reimplement downloading. A job is a whitelisted
CLI invocation (`process_scenes`, `catalog resync`, `doctor`) with a
server-written config file. This keeps one battle-tested execution path
(retry logic, memory budgeting, file locking, resume) and makes the web UI
*exactly as capable and exactly as safe* as the CLI. Progress is parsed from
the CLI's own structured log lines (`--- Batch i/n ---`, `[PHASE] …
tile=… batch=i/n`), which the pipeline already emits for its own diagnostics —
no protocol was invented.

Queueing is a bounded FIFO with configurable concurrency (default 1, because
the pipeline's own `parallel.max_workers` already parallelises *within* a
run; stacking runs multiplies memory). Jobs survive browser refreshes and
disconnects because they are server-owned; logs stream to per-job files and
are served incrementally.

### 2.3 API-first: the UI is a client, not the app

Every capability is an HTTP endpoint before it is a pixel. The SPA uses only
the public API; `curl` and CI scripts get the same one. This dissolves v1.0's
limitation #7 and gives automated/headless integration for free (see
`docs/webapp.md` §5 for scripted examples).

---

## 3. Technology stack and justification

| Layer | Choice | Why (in the context of satellite data) |
|---|---|---|
| Backend framework | **FastAPI** (optional extra `s1grits[web]`) | Typed request/response models, automatic OpenAPI docs at `/docs` (deliverable: API documentation is generated, not hand-maintained), tiny dependency surface on top of the existing scientific stack. |
| Server | **uvicorn**, bound to `127.0.0.1` by default | The workspace lives where the data lives (HPC login node, lab server); SSH port-forwarding is the deployment model. Public binding is an explicit opt-in paired with token auth. |
| Job execution | `subprocess` + threads (no Celery/Redis) | One machine, one workspace, a handful of long jobs — a broker adds an ops burden with zero benefit at this scale. The manager is ~300 lines and fully testable. |
| Geo maths | **pyproj / rasterio** (already dependencies) | UTM→WGS84 bounds for map display, lon/lat→pixel for time-series probes. No new geo deps. |
| Data access | **pandas / zarr** (already dependencies) | Catalog queries are DataFrame filters over mtime-cached parquet; time-series probes read `O(n_time)` elements from Zarr, never whole arrays. |
| Frontend | **Vanilla ES modules + Leaflet 1.9 (CDN)** — deliberately **no React/Vue/Node toolchain** | This is a Python scientific package installed with `pip`. A JS build chain would make every contributor install Node to change a button, and would rot independently of the Python release cycle. The UI's real complexity is server-side; the client is ~1,200 lines of framework-free code with zero build step, served from package data. Leaflet is the one exception because hand-rolling slippy-map maths is where framework-free stops being pragmatic. |
| Charts | Hand-rolled `<canvas>` line/strip charts | The two charts needed (point time-series, month coverage strip) are ~150 lines; Plotly (v1.0) shipped ~3.5 MB to render them. |

**What was deliberately rejected**

- *React + Vite + TypeScript*: the right call for a product team with a
  frontend owner; wrong for a research pipeline where the GUI must never be
  the reason a release stalls. The API-first design means a rich SPA can be
  added later without touching the server.
- *Tile server / dynamic COG tiling (titiler)*: valuable at global scale, but
  v2.3's visual unit is the per-month preview PNG the pipeline already
  generates (300 m), overlaid at the item's true bounds — sub-second loads
  with zero raster maths in the request path. COGs are served as files for
  QGIS/notebook use; dynamic tiling is a documented extension point.
- *WebSockets for progress*: 2-second polling of `/api/jobs` is simpler,
  proxy-friendly (SSH tunnels, JupyterHub proxies), and indistinguishable at
  this event rate. The incremental log endpoint (`?after=<line>`) keeps
  transfers O(new lines).

---

## 4. UI/UX enhancements over v1.0

1. **One workspace-centric screen** instead of five tool-centric tabs: map +
   filters + item table + detail panel + jobs drawer, all live at once. The
   v1.0 tabs' functionality maps as: Mapping/Tile → map + probe + detail;
   Catalog → workspace summary + resync job; Process/Mosaic → job composer.
2. **Progressive disclosure**: the map answers "what do I have and where";
   clicking a tile filters the table; clicking an item shows its preview on
   the map and its metadata; clicking the map inside a tile probes the Zarr
   time series at that point (with the `n_obs` confidence band plotted under
   it). Nothing requires a page transition.
3. **A real download manager**: queue of named jobs with per-tile batch
   progress bars, live log tail, cancel, and history — persistent across
   refreshes and shared between viewers of the same server.
4. **Month timeline strip**: per-month item counts rendered as a heat strip;
   brushing it sets the table/map date filter — the fastest way to spot the
   coverage gaps that `processing_report.json` records.
5. **Responsive layout**: CSS grid with two breakpoints — ≥1200 px (map +
   side panels), tablet 768–1200 px (stacked panels, collapsible drawer).
   No fixed pixel canvas as in v1.0's injected CSS.
6. **Headless parity**: every UI action has a copy-as-`curl` equivalent
   documented; the config composer pre-fills the same YAML the CLI reads.

## 5. Performance optimisations

- **mtime-cached catalog merge**: each tile's `catalog.parquet` is loaded
  once and re-read only when its mtime changes; queries are vectorised
  DataFrame filters with limit/offset pagination (bounded JSON payloads).
- **Pre-computed WGS84 bounds** per item at load time (vectorised per unique
  grid, not per row) so the map never blocks on reprojection.
- **Point probes read `O(n_time)` values** from Zarr (orthogonal indexing on
  one pixel), never a spatial slab; a 117-month probe is a few hundred KB of
  chunk reads.
- **Static previews over dynamic rendering**: the pipeline's 300 m PNGs are
  the map overlays; the browser caches them (`Cache-Control` on the asset
  route).
- **Incremental log streaming** and 2 s job polling keep steady-state
  network traffic to a few hundred bytes.
- **No framework rerenders**: DOM updates are targeted; the map and charts
  persist across interactions (v1.0's biggest interactive-latency cost).

## 6. Security considerations

Threat model: the server fronts a filesystem workspace and can launch heavy
processing; the risks are arbitrary path reads, arbitrary command execution,
and unwanted exposure of an unauthenticated control plane.

1. **Command whitelist**: jobs map to a fixed table of CLI argument vectors;
   user input is confined to a YAML config that is parsed with
   `yaml.safe_load`, schema-sanity-checked, and rewritten by the server with
   `output.base_dir` **forced to the workspace root** — a job cannot write
   outside the workspace, and no user string is ever joined into a shell.
2. **Path-traversal-safe assets**: `/api/asset` resolves the requested path
   and requires the result to be inside the workspace (`Path.resolve()` +
   `is_relative_to`); dotfiles and the job ledger are excluded.
3. **Local-first exposure**: default bind `127.0.0.1`. `--host 0.0.0.0`
   without a token is refused unless `--insecure` is explicit.
4. **Bearer-token auth** (`--token` / `S1GRITS_WEB_TOKEN`): constant-time
   comparison, required on every `/api/*` route when set.
5. **Credential hygiene**: job logs pass through the same redaction filter
   as v1.0's runner (`password/token/secret/api key → [REDACTED]`);
   `.netrc`-style Earthdata credentials never transit the API.
6. **No arbitrary config paths**: the server reads configs it wrote into
   `.webapp/jobs/<id>/`, never user-supplied filesystem paths.

## 7. Extensibility roadmap (explicit non-goals of v2.3, designed-for)

- **Dynamic COG tiles**: mount titiler alongside and point the map's overlay
  URL template at it — the item API already carries `cog_path` + bounds.
- **Multi-user auth**: replace the token dependency with OAuth middleware;
  the API surface is already session-free.
- **Rich SPA**: the OpenAPI schema is the contract; a React client can be
  developed against `/docs` without server changes.
- **STAC federation**: the workspace already emits STAC; a `/stac` mount
  serving the item JSONs is a routing exercise.
