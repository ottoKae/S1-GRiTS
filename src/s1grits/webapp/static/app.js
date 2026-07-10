/* S1-GRiTS web UI v2.3 — framework-free ES module client of the /api surface.
 *
 * Sections: api · state · workspace · tiles/map · burst layer · filters ·
 * items table · coverage matrix · timeline strip · tabs/detail panel ·
 * time-series probe · job composer · jobs drawer.
 * Every capability used here is a plain HTTP endpoint (see /docs), so any
 * behaviour in this file can be reproduced headlessly with curl.
 */

"use strict";

/* ================================ api ================================ */

const TOKEN = new URLSearchParams(location.search).get("token")
  || localStorage.getItem("s1grits_token") || "";
if (TOKEN) localStorage.setItem("s1grits_token", TOKEN);

async function api(path, opts = {}) {
  const headers = Object.assign(
    { "Content-Type": "application/json" },
    TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
    opts.headers || {},
  );
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch { /* noop */ }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

function assetUrl(tile, relpath) {
  const t = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : "";
  return `/api/asset/${encodeURIComponent(tile)}/${relpath}${t}`;
}

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const BASE_TITLE = document.title;
const DEFAULT_HINT = "Click a tile to filter · select a dataset, then click inside its footprint to probe";

/* =============================== state =============================== */

const state = {
  tiles: [],
  coverage: null,            // /api/coverage payload
  gaps: [],                  // [{tile_id, month}] — 0-count months inside a tile's span
  filters: { tile: "", product_type: "", direction: "", track: "", from: "", to: "" },
  page: { offset: 0, limit: 100, total: 0 },
  items: [],
  months: {},                // YYYY-MM -> count (of the filtered set)
  selected: null,            // selected item record
  probeMarker: null,
  jobs: [],
  logView: { jobId: null, after: 0, timer: null },
};

/* ============================ workspace ============================= */

async function loadWorkspace() {
  const [health, ws] = await Promise.all([api("/api/health"), api("/api/workspace")]);
  $("version-badge").textContent = `v${health.version}`;
  $("workspace-chips").innerHTML = `
    <span class="chip" title="${esc(ws.root)}">workspace <b>${esc(ws.root.split(/[\\/]/).pop())}</b></span>
    <span class="chip">tiles <b>${ws.n_tiles}</b></span>
    <span class="chip">items <b>${ws.n_items}</b></span>
    <span class="chip">disk free <b>${ws.disk_free_gb} GB</b></span>`;
}

/* ============================ tiles / map ============================ */

let map, tileLayerGroup, previewOverlay, burstLayer;

function initMap() {
  map = L.map("map", { zoomControl: true, attributionControl: true });
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap · CARTO",
  }).addTo(map);
  map.setView([0, 0], 3);
  tileLayerGroup = L.layerGroup().addTo(map);
  map.on("click", onMapClick);
}

function renderTiles() {
  tileLayerGroup.clearLayers();
  const list = $("tile-list");
  list.innerHTML = "";
  const boundsAll = [];
  for (const t of state.tiles) {
    // Sidebar entry
    const li = document.createElement("li");
    li.className = state.filters.tile === t.tile_id ? "active" : "";
    const warn = t.report && (t.report.n_incomplete || t.report.dropped_tracks?.length)
      ? `<span class="t-warn" title="${t.report.n_incomplete} incomplete acquisition(s)">⚠</span>` : "";
    li.innerHTML = `<span class="t-name">${esc(t.tile_id)} ${warn}</span>
      <span class="t-meta">${t.n_items} items<br>${esc(t.month_min || "")}→${esc(t.month_max || "")}</span>`;
    li.onclick = () => { setFilter("tile", state.filters.tile === t.tile_id ? "" : t.tile_id); };
    list.appendChild(li);

    // Map footprint. outline4326 is the DENSIFIED TRUE PERIMETER of the UTM
    // grid reprojected to WGS84 — a rectangle from bounds4326 would be the
    // axis-aligned bbox of that quad and render visibly skewed/oversized.
    if (t.bounds4326 || t.outline4326) {
      const active = state.filters.tile === t.tile_id;
      const style = {
        color: active ? "#6fe3b4" : "#4da3ff",
        weight: active ? 2.5 : 1.2,
        fillOpacity: active ? 0.12 : 0.05,
      };
      const shape = t.outline4326
        ? L.polygon(t.outline4326, style)
        : L.rectangle(t.bounds4326, style);
      shape.addTo(tileLayerGroup);
      boundsAll.push(shape.getBounds());
      shape.bindTooltip(`${t.tile_id} · ${t.n_items} items`, { sticky: true });
      shape.on("click", (e) => {
        L.DomEvent.stop(e);
        setFilter("tile", state.filters.tile === t.tile_id ? "" : t.tile_id);
      });
    }
  }
  if (boundsAll.length && !map._fittedOnce) {
    map.fitBounds(boundsAll.reduce((a, b) => a.extend(b)), { padding: [24, 24] });
    map._fittedOnce = true;
  }
}

async function showPreviewOverlay(item) {
  if (previewOverlay) { map.removeLayer(previewOverlay); previewOverlay = null; }
  if (!(item && item.preview_path)) return;
  // The catalog row's bounds4326 describe the FULL master grid; the preview
  // PNG covers only the tile-clipped crop. Ask the server for the asset's
  // TRUE footprint so the image is not stretched over the grid extent.
  let bounds = item.bounds4326;
  try {
    const b = await api(
      `/api/asset-bounds/${encodeURIComponent(item.tile_id)}/${item.preview_path}`);
    if (b.bounds4326) bounds = b.bounds4326;
  } catch { /* fall back to grid bounds */ }
  if (!bounds) return;
  if (state.selected !== item) return;  // stale response after re-selection
  previewOverlay = L.imageOverlay(
    assetUrl(item.tile_id, item.preview_path), bounds,
    { opacity: 0.85, interactive: false },
  ).addTo(map);
  map.fitBounds(bounds, { padding: [30, 30] });
}

async function onMapClick(e) {
  const it = state.selected;
  if (!it || !it.zarr_path) return;
  const b = it.bounds4326;
  if (b && (e.latlng.lat < b[0][0] || e.latlng.lat > b[1][0]
         || e.latlng.lng < b[0][1] || e.latlng.lng > b[1][1])) return;
  if (state.probeMarker) map.removeLayer(state.probeMarker);
  state.probeMarker = L.circleMarker(e.latlng, {
    radius: 6, color: "#ffb454", weight: 2, fillOpacity: 0.6,
  }).addTo(map);
  await loadProbe(it, e.latlng.lng, e.latlng.lat);
}

/* ---------- burst footprint reference layer (toggleable) ---------- */

let burstCache = null;   // { key, geojson } — one fetch per tile set

async function updateBurstLayer() {
  if (burstLayer) { map.removeLayer(burstLayer); burstLayer = null; }
  if (!$("layer-bursts").checked) { $("map-hint").textContent = DEFAULT_HINT; return; }
  if (!state.tiles.length) {
    $("map-hint").textContent = "No tiles in the workspace yet — burst footprints need at least one tile";
    return;
  }
  const key = state.tiles.map((t) => t.tile_id).sort().join(",");
  try {
    if (!burstCache || burstCache.key !== key) {
      $("map-hint").textContent = "Loading burst footprints…";
      const geojson = await api(`/api/bursts?tiles=${encodeURIComponent(key)}`);
      burstCache = { key, geojson };
    }
    if (!$("layer-bursts").checked) return;   // toggled off while loading
    burstLayer = L.geoJSON(burstCache.geojson, {
      style: { color: "#ffb454", weight: 1, dashArray: "4 3", fill: false, opacity: 0.75 },
      onEachFeature: (f, layer) => {
        layer.bindTooltip(
          `${f.properties.jpl_burst_id} · TK${f.properties.track}`, { sticky: true });
        layer.on("click", (e) => onMapClick(e));   // keep probing usable through the overlay
      },
    }).addTo(map);
    const n = (burstCache.geojson.features || []).length;
    $("map-hint").textContent = `${n} burst footprint(s) · ${DEFAULT_HINT}`;
  } catch (err) {
    $("map-hint").textContent = `Burst layer: ${err.message}`;
  }
}

/* ============================== filters ============================== */

function setFilter(key, value) {
  state.filters[key] = value;
  state.page.offset = 0;
  syncFilterControls();
  refresh();
}

function syncFilterControls() {
  $("f-tile").value = state.filters.tile;
  $("f-product").value = state.filters.product_type;
  $("f-direction").value = state.filters.direction;
  $("f-track").value = state.filters.track;
  $("f-from").value = state.filters.from;
  $("f-to").value = state.filters.to;
}

function populateFilterOptions() {
  const opt = (v, label) => `<option value="${esc(v)}">${esc(label ?? v)}</option>`;
  $("f-tile").innerHTML = opt("", "All tiles")
    + state.tiles.map((t) => opt(t.tile_id)).join("");
  const products = new Set(), dirs = new Set(), tracks = new Set();
  for (const t of state.tiles) {
    (t.product_types || []).forEach((p) => products.add(p));
    (t.directions || []).forEach((d) => dirs.add(d));
    (t.tracks || []).forEach((k) => tracks.add(k));
  }
  $("f-product").innerHTML = opt("", "All products") + [...products].sort().map((p) => opt(p)).join("");
  $("f-direction").innerHTML = opt("", "All directions") + [...dirs].sort().map((d) => opt(d)).join("");
  $("f-track").innerHTML = opt("", "All tracks") + [...tracks].sort((a, b) => a - b).map((k) => opt(k, `TK${k}`)).join("");
}

function bindFilterEvents() {
  $("f-tile").onchange = (e) => setFilter("tile", e.target.value);
  $("f-product").onchange = (e) => setFilter("product_type", e.target.value);
  $("f-direction").onchange = (e) => setFilter("direction", e.target.value);
  $("f-track").onchange = (e) => setFilter("track", e.target.value);
  $("f-from").onchange = (e) => setFilter("from", e.target.value);
  $("f-to").onchange = (e) => setFilter("to", e.target.value);
  $("btn-clear-filters").onclick = () => {
    state.filters = { tile: "", product_type: "", direction: "", track: "", from: "", to: "" };
    state.page.offset = 0;
    syncFilterControls();
    refresh();
  };
}

/* ============================ items table ============================ */

async function loadItems() {
  const f = state.filters;
  const q = new URLSearchParams();
  if (f.tile) q.set("tile", f.tile);
  if (f.product_type) q.set("product_type", f.product_type);
  if (f.direction) q.set("direction", f.direction);
  if (f.track) q.set("track", f.track);
  if (f.from) q.set("month_from", f.from);
  if (f.to) q.set("month_to", f.to);
  q.set("limit", state.page.limit);
  q.set("offset", state.page.offset);
  const data = await api(`/api/items?${q}`);
  state.items = data.items;
  state.months = data.months;
  state.page.total = data.total;
  renderItems();
  drawTimeline();
}

function renderItems() {
  const body = $("items-body");
  body.innerHTML = "";
  for (const it of state.items) {
    const tr = document.createElement("tr");
    if (state.selected && state.selected.item_id === it.item_id) tr.className = "active";
    const assets = [];
    if (it.preview_path) assets.push(`<a href="${assetUrl(it.tile_id, it.preview_path)}" target="_blank">png</a>`);
    if (it.cog_path) assets.push(`<a href="${assetUrl(it.tile_id, it.cog_path)}" target="_blank">cog</a>`);
    if (it.zarr_path) assets.push(`<span class="muted" title="${esc(it.zarr_path)}">zarr</span>`);
    tr.innerHTML = `
      <td title="${esc(it.item_id)}">${esc((it.item_id || "").slice(0, 44))}</td>
      <td>${esc(it.tile_id)}</td>
      <td>${esc(it.product_type)}</td>
      <td>${it.track != null ? "TK" + esc(it.track) : "—"}</td>
      <td>${esc(it.month || (it.datetime || "").slice(0, 10))}</td>
      <td>${it.n_scenes ?? "—"}</td>
      <td class="asset-links">${assets.join(" ")}</td>`;
    tr.onclick = (e) => { if (e.target.tagName !== "A") selectItem(it); };
    body.appendChild(tr);
  }
  $("items-count").textContent = `(${state.page.total})`;
  const { offset, limit, total } = state.page;
  $("pg-label").textContent = total
    ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total}` : "no items";
  $("pg-prev").disabled = offset <= 0;
  $("pg-next").disabled = offset + limit >= total;
}

function bindPager() {
  $("pg-prev").onclick = () => { state.page.offset = Math.max(0, state.page.offset - state.page.limit); loadItems(); };
  $("pg-next").onclick = () => { state.page.offset += state.page.limit; loadItems(); };
}

/* ========================= coverage matrix ========================== */

/* Tiles × months heat-strip: the spatial↔temporal bridge. Each row is a
 * tile, each column a month on a CONTINUOUS axis; red cells are months with
 * zero composites inside that tile's own [first, last] span — the gaps the
 * "Patch coverage gaps" button turns into a download config. */

function monthAxis(first, last) {
  const axis = [];
  let [y, m] = first.split("-").map(Number);
  const [ey, em] = last.split("-").map(Number);
  while (y < ey || (y === ey && m <= em)) {
    axis.push(`${y}-${String(m).padStart(2, "0")}`);
    m++; if (m > 12) { m = 1; y++; }
  }
  return axis;
}

async function loadCoverage() {
  try { state.coverage = await api("/api/coverage"); }
  catch { state.coverage = null; }
  drawCoverage();
}

function drawCoverage() {
  const canvas = $("coverage-matrix");
  const ctx = canvas.getContext("2d");
  const dpr = devicePixelRatio;
  const tiles = state.coverage ? state.coverage.tiles : [];
  const monthsAll = state.coverage ? state.coverage.months_all : [];
  state.gaps = [];
  if (!tiles.length || !monthsAll.length) {
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = 24 * dpr;
    canvas.style.height = "24px";
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    $("coverage-caption").textContent = "no coverage yet — queue a download to populate the workspace";
    canvas._grid = null;
    return;
  }

  const axis = monthAxis(monthsAll[0], monthsAll[monthsAll.length - 1]);
  const rowH = 16, topPad = 14, gutter = 58;
  const cssH = topPad + tiles.length * rowH + 2;
  const W = canvas.width = canvas.clientWidth * dpr;
  canvas.height = cssH * dpr;
  canvas.style.height = `${cssH}px`;
  ctx.clearRect(0, 0, W, canvas.height);
  const cw = (W - gutter * dpr) / axis.length;

  // Year ticks along the top
  ctx.fillStyle = "#93a1b8";
  ctx.font = `${9 * dpr}px sans-serif`;
  axis.forEach((mo, i) => {
    if (mo.endsWith("-01")) {
      ctx.fillText(mo.slice(0, 4), gutter * dpr + i * cw, 10 * dpr);
    }
  });

  const maxCount = Math.max(1, ...tiles.flatMap((t) => Object.values(t.months)));
  let nGaps = 0;
  tiles.forEach((t, r) => {
    const y = (topPad + r * rowH) * dpr;
    const have = Object.keys(t.months).filter((m) => t.months[m] > 0).sort();
    const span = have.length ? [have[0], have[have.length - 1]] : null;
    // Tile label; highlighted when it's the active filter
    ctx.fillStyle = state.filters.tile === t.tile_id ? "#6fe3b4" : "#93a1b8";
    ctx.font = `${10 * dpr}px sans-serif`;
    ctx.fillText(t.tile_id, 2 * dpr, y + 12 * dpr);
    axis.forEach((mo, i) => {
      const count = t.months[mo] || 0;
      const inSpan = span && mo >= span[0] && mo <= span[1];
      if (count > 0) {
        ctx.fillStyle = `rgba(77,163,255,${0.3 + 0.7 * count / maxCount})`;
      } else if (inSpan) {
        ctx.fillStyle = "rgba(255,107,107,.55)";   // gap: hole inside the tile's own span
        state.gaps.push({ tile_id: t.tile_id, month: mo });
        nGaps++;
      } else {
        ctx.fillStyle = "#1d2739";                 // outside span: nothing expected
      }
      ctx.fillRect(gutter * dpr + i * cw + 0.5, y, Math.max(cw - 1, 1), (rowH - 2) * dpr);
    });
  });
  $("coverage-caption").textContent = nGaps
    ? `${nGaps} gap month(s) detected — use “Patch coverage gaps” to queue a fill run`
    : "each row is a tile; no temporal gaps inside any tile's span";
  canvas._grid = { axis, tiles, rowH, topPad, gutter, cw, dpr };
}

function bindCoverage() {
  $("coverage-matrix").onclick = (e) => {
    const g = $("coverage-matrix")._grid;
    if (!g) return;
    const x = e.offsetX * g.dpr, y = e.offsetY;
    if (x < g.gutter * g.dpr || y < g.topPad) return;
    const col = Math.min(g.axis.length - 1, Math.floor((x - g.gutter * g.dpr) / g.cw));
    const row = Math.floor((y - g.topPad) / g.rowH);
    if (row < 0 || row >= g.tiles.length) return;
    // Drill into that tile+month in the datasets table below
    state.filters.tile = g.tiles[row].tile_id;
    state.filters.from = state.filters.to = g.axis[col];
    state.page.offset = 0;
    syncFilterControls();
    refresh();
  };
}

/* ============================= timeline ============================= */

function drawTimeline() {
  const canvas = $("timeline");
  const ctx = canvas.getContext("2d");
  const w = canvas.width = canvas.clientWidth * devicePixelRatio;
  const h = canvas.height = 56 * devicePixelRatio;
  ctx.clearRect(0, 0, w, h);
  const months = Object.keys(state.months).sort();
  $("timeline-caption").textContent = months.length
    ? `${months[0]} → ${months[months.length - 1]} · click a month to filter, shift-click to end a range`
    : "no months in the current filter";
  if (!months.length) { canvas._months = []; return; }

  // Continuous month axis between min and max so gaps are VISIBLE.
  const axis = monthAxis(months[0], months[months.length - 1]);
  const max = Math.max(...Object.values(state.months));
  const bw = w / axis.length;
  axis.forEach((mo, i) => {
    const count = state.months[mo] || 0;
    const inRange = (!state.filters.from || mo >= state.filters.from)
      && (!state.filters.to || mo <= state.filters.to);
    const frac = count / max;
    ctx.fillStyle = count === 0 ? "#26314a"
      : inRange ? `rgba(77,163,255,${0.25 + 0.75 * frac})`
      : `rgba(147,161,184,${0.15 + 0.4 * frac})`;
    ctx.fillRect(i * bw + 1, h * (1 - Math.max(frac, 0.08)) , bw - 2, h * Math.max(frac, 0.08));
    if (mo.endsWith("-01")) {
      ctx.fillStyle = "#93a1b8";
      ctx.font = `${10 * devicePixelRatio}px sans-serif`;
      ctx.fillText(mo.slice(0, 4), i * bw + 2, 11 * devicePixelRatio);
    }
  });
  canvas._months = axis;
  canvas._bw = bw;
}

function bindTimeline() {
  const canvas = $("timeline");
  canvas.onclick = (e) => {
    const axis = canvas._months || [];
    if (!axis.length) return;
    const x = (e.offsetX * devicePixelRatio);
    const mo = axis[Math.min(axis.length - 1, Math.floor(x / canvas._bw))];
    if (e.shiftKey && state.filters.from) {
      state.filters.to = mo >= state.filters.from ? mo : state.filters.from;
    } else if (state.filters.from === mo && state.filters.to === mo) {
      state.filters.from = state.filters.to = "";   // toggle off
    } else {
      state.filters.from = state.filters.to = mo;
    }
    state.page.offset = 0;
    syncFilterControls();
    loadItems();
  };
}

/* ===================== right panel: tabs + detail ==================== */

function switchTab(name) {
  document.querySelectorAll(".tabs .tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  $("tabbody-download").classList.toggle("hidden", name !== "download");
  $("tabbody-item").classList.toggle("hidden", name !== "item");
}
document.querySelectorAll(".tabs .tab").forEach((t) => {
  t.onclick = () => switchTab(t.dataset.tab);
});

function selectItem(item) {
  state.selected = item;
  renderItems();
  switchTab("item");
  $("detail-empty-hint").hidden = true;
  $("detail-title").textContent = item.item_id || "Item";
  const img = $("detail-preview");
  if (item.preview_path) {
    img.src = assetUrl(item.tile_id, item.preview_path);
    img.hidden = false;
  } else { img.hidden = true; }

  const rows = [
    ["Tile", item.tile_id], ["Product", item.product_label || item.product_type],
    ["Direction", item.flight_direction], ["Track", item.track != null ? `TK${item.track}` : null],
    ["Month", item.month], ["Scenes", item.n_scenes],
    ["CRS", item.crs], ["Grid", item.width && `${item.width}×${item.height}`],
    ["Bands", (() => { try { return JSON.parse(item.bands || "[]").join(", "); } catch { return item.bands; } })()],
  ];
  $("detail-meta").innerHTML = rows
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("");

  const actions = [];
  if (item.cog_path) actions.push(`<a class="btn small" href="${assetUrl(item.tile_id, item.cog_path)}" download>⬇ COG</a>`);
  if (item.preview_path) actions.push(`<a class="btn small ghost" href="${assetUrl(item.tile_id, item.preview_path)}" target="_blank">Preview ↗</a>`);
  $("detail-actions").innerHTML = actions.join(" ");
  $("probe-panel").hidden = true;
  $("map-hint").textContent = item.zarr_path
    ? `Probing ${item.item_id} — click inside the highlighted footprint`
    : "This item has no Zarr store to probe";
  showPreviewOverlay(item);
}

function clearSelection() {
  state.selected = null;
  $("detail-empty-hint").hidden = false;
  $("detail-title").textContent = "No dataset selected";
  $("detail-preview").hidden = true;
  $("detail-meta").innerHTML = "";
  $("detail-actions").innerHTML = "";
  $("probe-panel").hidden = true;
  $("map-hint").textContent = DEFAULT_HINT;
  showPreviewOverlay(null);
  if (state.probeMarker) { map.removeLayer(state.probeMarker); state.probeMarker = null; }
  renderItems();
}
$("detail-close").onclick = clearSelection;

/* ======================== time-series probe ========================= */

const BAND_COLORS = { VV_dB: "#4da3ff", VH_dB: "#6fe3b4", Ratio: "#ffb454", RVI: "#c792ea" };

async function loadProbe(item, lon, lat) {
  $("probe-caption").textContent = "loading…";
  $("probe-panel").hidden = false;
  try {
    const q = new URLSearchParams({
      tile: item.tile_id, zarr_path: item.zarr_path,
      lon: lon.toFixed(6), lat: lat.toFixed(6),
    });
    const ts = await api(`/api/timeseries?${q}`);
    drawProbe(ts);
    $("probe-caption").textContent =
      `${lat.toFixed(4)}, ${lon.toFixed(4)} · px(${ts.pixel.row},${ts.pixel.col}) · ${ts.time.length} steps`;
  } catch (err) {
    $("probe-caption").textContent = err.message;
  }
}

function drawProbe(ts) {
  const canvas = $("probe-chart");
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth * devicePixelRatio;
  const H = canvas.height = 220 * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  const padL = 42 * devicePixelRatio, padB = 16 * devicePixelRatio, padT = 14 * devicePixelRatio;

  const bands = Object.entries(ts.bands).filter(([k]) => k !== "n_obs");
  const values = bands.flatMap(([, v]) => v).filter((v) => v != null);
  if (!values.length) return;
  let lo = Math.min(...values), hi = Math.max(...values);
  if (hi - lo < 1e-6) { hi += 1; lo -= 1; }
  const n = ts.time.length;
  const x = (i) => padL + (W - padL - 6) * (n === 1 ? 0.5 : i / (n - 1));
  const y = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo));

  // axes + gridlines
  ctx.strokeStyle = "#2a3549"; ctx.fillStyle = "#93a1b8";
  ctx.font = `${10 * devicePixelRatio}px sans-serif`;
  for (let g = 0; g <= 4; g++) {
    const v = lo + (hi - lo) * g / 4;
    ctx.beginPath(); ctx.moveTo(padL, y(v)); ctx.lineTo(W, y(v)); ctx.stroke();
    ctx.fillText(v.toFixed(1), 4, y(v) + 3 * devicePixelRatio);
  }
  // series
  let legendX = padL;
  for (const [name, vals] of bands) {
    const color = BAND_COLORS[name] || "#e8edf6";
    ctx.strokeStyle = color; ctx.lineWidth = 1.6 * devicePixelRatio;
    ctx.beginPath();
    let pen = false;
    vals.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      if (!pen) { ctx.moveTo(x(i), y(v)); pen = true; } else { ctx.lineTo(x(i), y(v)); }
    });
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.fillText(name, legendX, 10 * devicePixelRatio);
    legendX += ctx.measureText(name).width + 14 * devicePixelRatio;
  }
  // x labels: first / mid / last
  ctx.fillStyle = "#93a1b8";
  [[0, "left"], [Math.floor(n / 2), "center"], [n - 1, "right"]].forEach(([i, align]) => {
    ctx.textAlign = align;
    ctx.fillText((ts.time[i] || "").slice(0, 7), x(i), H - 4);
  });
  ctx.textAlign = "left";

  drawNobs(ts, x);
}

function drawNobs(ts, x) {
  const canvas = $("probe-nobs");
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth * devicePixelRatio;
  const H = canvas.height = 46 * devicePixelRatio;
  ctx.clearRect(0, 0, W, H);
  const nobs = ts.bands.n_obs;
  ctx.fillStyle = "#93a1b8";
  ctx.font = `${9 * devicePixelRatio}px sans-serif`;
  if (!nobs) { ctx.fillText("n_obs not present in this store", 6, 12 * devicePixelRatio); return; }
  ctx.fillText("n_obs", 4, 10 * devicePixelRatio);
  const max = Math.max(1, ...nobs.filter((v) => v != null));
  const bw = Math.max(2, (W - 48) / nobs.length - 1);
  nobs.forEach((v, i) => {
    const val = v || 0;
    ctx.fillStyle = val === 0 ? "#33415c" : `rgba(111,227,180,${0.3 + 0.7 * val / max})`;
    const bh = (H - 14 * devicePixelRatio) * (val / max || 0.06);
    ctx.fillRect(x(i) - bw / 2, H - bh, bw, bh);
  });
}

/* ==================== job composer (Download tab) ==================== */

async function initComposer() {
  const types = await api("/api/job-types");
  $("job-type").innerHTML = Object.entries(types)
    .map(([k, v]) => `<option value="${esc(k)}" data-needs-config="${v.needs_config}">${esc(v.title)} (${esc(k)})</option>`)
    .join("");
  if (!$("job-config").value) {
    $("job-config").value = (await api("/api/config-template")).yaml;
  }
  syncConfigVisibility();
}

function syncConfigVisibility() {
  const sel = $("job-type").selectedOptions[0];
  const needs = sel && sel.dataset.needsConfig === "true";
  $("job-config-label").style.display = needs ? "" : "none";
  $("job-quickfill").style.display = needs ? "" : "none";
}
$("job-type").onchange = syncConfigVisibility;

/* --- quick-fill: form -> YAML (bridges the panel to the CLI config) --- */
function composeQuickfillYaml() {
  const tiles = $("qf-tiles").value.split(",").map((t) => t.trim().toUpperCase())
    .filter(Boolean);
  const dir = $("qf-direction").value;
  const mode = $("qf-time-mode").value;
  const years = $("qf-years").value.split(",").map((y) => parseInt(y, 10))
    .filter((y) => y >= 2014 && y <= 2100);
  const months = $("qf-months").value.split(",").map((m) => parseInt(m, 10))
    .filter((m) => m >= 1 && m <= 12);
  const cog = $("qf-cog").checked, png = $("qf-preview").checked;
  if (!tiles.length) throw new Error("Quick fill: enter at least one MGRS tile");
  if (mode === "years" && !years.length) throw new Error("Quick fill: enter year(s)");

  const time = mode === "full"
    ? `  full: ${years[0] || new Date().getFullYear()}`
    : `  years: [${years.join(", ")}]`
      + (months.length ? `\n  months: [${months.join(", ")}]` : "");
  return `workflow: "scenes"

roi:
  manual_mgrs_tiles:
${tiles.map((t) => `    - "${t}"`).join("\n")}
  flight_direction: "${dir}"
  polarization: "VV+VH"

time:
${time}

output:
  base_dir: "."          # forced to the server workspace on submit
  existing_store: "resume"
  existing_month: "skip"
  formats: {cog: ${cog}, preview: ${png}}

processing:
  target_resolution: 30.0
  tile_clip: true
  monthly:
    enabled: true
    only: true
    composite_method: "nanmedian"
    generate_cog: ${cog}
    generate_preview: ${png}
    blockwise_threads: 2

memory:
  max_memory_gb: 'auto'
  batch_strategy: 'auto'
  max_download_workers: 8

parallel:
  enabled: true
  max_workers: 2
`;
}

$("qf-apply").onclick = () => {
  const box = $("job-error");
  try {
    $("job-config").value = composeQuickfillYaml();
    box.classList.add("hidden");
    if (!$("job-title").value) {
      $("job-title").value =
        `${$("qf-tiles").value.split(",").length} tile(s) · ${$("qf-time-mode").value}`;
    }
  } catch (err) {
    box.textContent = err.message;
    box.classList.remove("hidden");
  }
};
$("qf-time-mode").onchange = () => {
  $("qf-months").disabled = $("qf-time-mode").value === "full";
};

/* --- gap fill: coverage matrix red cells -> prefilled download run ---
 * Requests the UNION of gap years × gap months, a superset of the exact
 * (tile, month) holes — harmless because existing_month: "skip" makes the
 * run a no-op for months that are already on disk. */
$("btn-patch-gaps").onclick = () => {
  const box = $("job-error");
  const tileFilter = state.filters.tile;
  const gaps = state.gaps.filter((g) => !tileFilter || g.tile_id === tileFilter);
  if (!gaps.length) {
    box.textContent = tileFilter
      ? `No coverage gaps detected for ${tileFilter}.`
      : "No coverage gaps detected — the matrix has no red cells.";
    box.classList.remove("hidden");
    return;
  }
  const tiles = [...new Set(gaps.map((g) => g.tile_id))];
  const years = [...new Set(gaps.map((g) => g.month.slice(0, 4)))].sort();
  const months = [...new Set(gaps.map((g) => +g.month.slice(5)))].sort((a, b) => a - b);
  $("qf-tiles").value = tiles.join(", ");
  $("qf-time-mode").value = "years";
  $("qf-months").disabled = false;
  $("qf-years").value = years.join(", ");
  $("qf-months").value = months.join(", ");
  try {
    $("job-config").value = composeQuickfillYaml();
    if (!$("job-title").value) {
      $("job-title").value = `Gap fill · ${tiles.join("+")} · ${gaps.length} month(s)`;
    }
    box.classList.add("hidden");
    switchTab("download");
  } catch (err) {
    box.textContent = err.message;
    box.classList.remove("hidden");
  }
};

$("job-submit").onclick = async () => {
  const sel = $("job-type").selectedOptions[0];
  const box = $("job-error");
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        type: $("job-type").value,
        title: $("job-title").value || undefined,
        config_yaml: sel.dataset.needsConfig === "true" ? $("job-config").value : undefined,
      }),
    });
    box.classList.add("hidden");
    $("job-title").value = "";
    await pollJobs();
    openDrawer();          // show the queued job's progress immediately
  } catch (err) {
    box.textContent = err.message;
    box.classList.remove("hidden");
  }
};

/* ============================ jobs drawer ============================ */

const drawer = $("jobs-drawer"), scrim = $("drawer-scrim");
function openDrawer() {
  drawer.classList.remove("hidden");
  scrim.classList.remove("hidden");
  pollJobs();
}
$("btn-jobs").onclick = openDrawer;
$("job-status-chip").onclick = openDrawer;
const closeDrawer = () => { drawer.classList.add("hidden"); scrim.classList.add("hidden"); stopLogView(); };
$("drawer-close").onclick = closeDrawer;
scrim.onclick = closeDrawer;

let hadActiveJobs = false;

async function pollJobs() {
  try { state.jobs = await api("/api/jobs"); } catch { return; }
  renderJobs();
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  const badge = $("jobs-active-badge");
  badge.textContent = active;
  badge.classList.toggle("hidden", active === 0);
  updateJobChip();
  // A run just finished: new composites may exist — refresh the workspace view.
  if (hadActiveJobs && active === 0) { loadWorkspace(); refresh(); }
  hadActiveJobs = active > 0;
}
setInterval(pollJobs, 2500);

/* Always-visible topbar chip: the current job's title/progress/duration
 * without opening the drawer; the browser tab title mirrors it. */
function updateJobChip() {
  const chip = $("job-status-chip");
  const running = state.jobs.find((j) => j.status === "running");
  const queued = state.jobs.filter((j) => j.status === "queued").length;
  if (!running && !queued) {
    chip.classList.add("hidden");
    document.title = BASE_TITLE;
    return;
  }
  if (running) {
    const pct = running.progress?.pct;
    chip.textContent = `▶ ${running.title}${pct != null ? ` · ${pct}%` : ""} · ${fmtDuration(running)}`
      + (queued ? ` · +${queued} queued` : "");
    document.title = `${pct != null ? `${pct}%` : "▶"} ${running.title} — ${BASE_TITLE}`;
  } else {
    chip.textContent = `${queued} job(s) queued`;
    document.title = `${queued} queued — ${BASE_TITLE}`;
  }
  chip.classList.remove("hidden");
}

function fmtDuration(j) {
  if (!j.started_at) return "";
  const end = j.ended_at || Date.now() / 1000;
  const s = Math.max(0, Math.round(end - j.started_at));
  return `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function renderJobs() {
  const ul = $("job-list");
  ul.innerHTML = "";
  if (!state.jobs.length) {
    ul.innerHTML = `<li class="muted">No jobs yet — queue one from the <b>Process &amp; download</b> panel.</li>`;
  }
  for (const j of state.jobs) {
    const li = document.createElement("li");
    li.className = "job-card";
    const pct = j.progress?.pct;
    const tiles = Object.entries(j.progress?.per_tile || {})
      .filter(([t]) => t !== "_run")
      .map(([t, [c, n]]) => `${t} ${c}/${n}`).join(" · ");
    li.innerHTML = `
      <div class="j-head">
        <span class="j-title">${esc(j.title)}</span>
        <span class="status-pill status-${esc(j.status)}">${esc(j.status)}</span>
      </div>
      <div class="j-sub">${esc(j.type)} · ${fmtDuration(j)} ${tiles ? "· " + esc(tiles) : ""}
        ${j.error ? `<br><span style="color:var(--error)">${esc(j.error)}</span>` : ""}</div>
      ${pct != null ? `<div class="progress"><div style="width:${pct}%"></div></div>` : ""}
      <div class="j-actions">
        <button class="btn ghost small" data-log="${esc(j.id)}">Log</button>
        ${(j.status === "running" || j.status === "queued")
          ? `<button class="btn danger small" data-cancel="${esc(j.id)}">Cancel</button>` : ""}
      </div>`;
    li.querySelector("[data-log]").onclick = () => openLogView(j);
    const cancelBtn = li.querySelector("[data-cancel]");
    if (cancelBtn) cancelBtn.onclick = async () => {
      await api(`/api/jobs/${j.id}/cancel`, { method: "POST" });
      pollJobs();
    };
    ul.appendChild(li);
  }
}

/* --- incremental log viewer --- */
function openLogView(job) {
  stopLogView();
  state.logView = { jobId: job.id, after: 0, timer: null };
  $("job-log-title").textContent = `Log — ${job.title}`;
  $("job-log").textContent = "";
  $("job-log-wrap").classList.remove("hidden");
  const tick = async () => {
    try {
      const data = await api(`/api/jobs/${state.logView.jobId}/log?after=${state.logView.after}`);
      if (data.lines.length) {
        $("job-log").textContent += data.lines.join("\n") + "\n";
        $("job-log").scrollTop = $("job-log").scrollHeight;
        state.logView.after = data.next;
      }
      if (data.status === "running" || data.status === "queued") {
        state.logView.timer = setTimeout(tick, 1500);
      }
    } catch { /* viewer closed or job gone */ }
  };
  tick();
}
function stopLogView() {
  if (state.logView.timer) clearTimeout(state.logView.timer);
  state.logView = { jobId: null, after: 0, timer: null };
  $("job-log-wrap").classList.add("hidden");
}
$("job-log-close").onclick = stopLogView;

/* =============================== boot =============================== */

async function refresh() {
  state.tiles = await api("/api/tiles");
  // Brand-new/empty workspace: point the user at the download panel — the
  // path to a first dataset starts there, not in an empty map.
  const empty = !state.tiles.length;
  $("empty-cta").classList.toggle("hidden", !empty);
  if (empty) switchTab("download");
  populateFilterOptions();
  syncFilterControls();
  renderTiles();
  await Promise.all([loadItems(), loadCoverage()]);
  await updateBurstLayer();
}

$("btn-refresh").onclick = () => { loadWorkspace(); refresh(); };
$("layer-bursts").onchange = () => updateBurstLayer();
window.addEventListener("resize", () => { drawTimeline(); drawCoverage(); });

initMap();
bindFilterEvents();
bindPager();
bindTimeline();
bindCoverage();
loadWorkspace().catch((e) => {
  $("workspace-chips").innerHTML = `<span class="chip" style="color:var(--error)">${esc(e.message)}</span>`;
});
refresh().catch((e) => {
  $("items-count").textContent = e.message;
});
initComposer().catch((e) => {
  const box = $("job-error");
  box.textContent = `Job composer unavailable: ${e.message}`;
  box.classList.remove("hidden");
});
pollJobs();
