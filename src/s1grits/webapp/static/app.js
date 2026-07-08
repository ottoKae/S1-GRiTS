/* S1-GRiTS web UI v2.3 — framework-free ES module client of the /api surface.
 *
 * Sections: api · state · workspace · tiles/map · filters · items table ·
 * timeline strip · detail panel · time-series probe · jobs drawer.
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

/* =============================== state =============================== */

const state = {
  tiles: [],
  filters: { tile: "", product_type: "", direction: "", track: "", from: "", to: "" },
  page: { offset: 0, limit: 100, total: 0 },
  items: [],
  months: {},               // YYYY-MM -> count (of the filtered set)
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

let map, tileLayerGroup, previewOverlay;

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

    // Map footprint
    if (t.bounds4326) {
      boundsAll.push(t.bounds4326);
      const active = state.filters.tile === t.tile_id;
      const rect = L.rectangle(t.bounds4326, {
        color: active ? "#6fe3b4" : "#4da3ff",
        weight: active ? 2.5 : 1.2,
        fillOpacity: active ? 0.12 : 0.05,
      }).addTo(tileLayerGroup);
      rect.bindTooltip(`${t.tile_id} · ${t.n_items} items`, { sticky: true });
      rect.on("click", (e) => {
        L.DomEvent.stop(e);
        setFilter("tile", state.filters.tile === t.tile_id ? "" : t.tile_id);
      });
    }
  }
  if (boundsAll.length && !map._fittedOnce) {
    map.fitBounds(boundsAll.flat(), { padding: [24, 24] });
    map._fittedOnce = true;
  }
}

function showPreviewOverlay(item) {
  if (previewOverlay) { map.removeLayer(previewOverlay); previewOverlay = null; }
  if (item && item.preview_path && item.bounds4326) {
    previewOverlay = L.imageOverlay(
      assetUrl(item.tile_id, item.preview_path), item.bounds4326,
      { opacity: 0.85, interactive: false },
    ).addTo(map);
    map.fitBounds(item.bounds4326, { padding: [30, 30] });
  }
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
  const axis = [];
  let [y, m] = months[0].split("-").map(Number);
  const [ey, em] = months[months.length - 1].split("-").map(Number);
  while (y < ey || (y === ey && m <= em)) {
    axis.push(`${y}-${String(m).padStart(2, "0")}`);
    m++; if (m > 12) { m = 1; y++; }
  }
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

/* =========================== detail panel =========================== */

function selectItem(item) {
  state.selected = item;
  renderItems();
  const panel = $("detail-panel");
  panel.hidden = false;
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

$("detail-close").onclick = () => {
  state.selected = null;
  $("detail-panel").hidden = true;
  showPreviewOverlay(null);
  if (state.probeMarker) { map.removeLayer(state.probeMarker); state.probeMarker = null; }
  renderItems();
};

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

/* ============================ jobs drawer ============================ */

const drawer = $("jobs-drawer"), scrim = $("drawer-scrim");
$("btn-jobs").onclick = () => { drawer.classList.remove("hidden"); scrim.classList.remove("hidden"); pollJobs(); };
const closeDrawer = () => { drawer.classList.add("hidden"); scrim.classList.add("hidden"); stopLogView(); };
$("drawer-close").onclick = closeDrawer;
scrim.onclick = closeDrawer;

async function pollJobs() {
  try { state.jobs = await api("/api/jobs"); } catch { return; }
  renderJobs();
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  const badge = $("jobs-active-badge");
  badge.textContent = active;
  badge.classList.toggle("hidden", active === 0);
}
setInterval(pollJobs, 2500);

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
    ul.innerHTML = `<li class="muted">No jobs yet — queue one with “＋ New job”.</li>`;
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

/* --- new-job modal --- */
const modal = $("job-modal");
$("btn-new-job").onclick = async () => {
  modal.classList.remove("hidden");
  $("job-error").classList.add("hidden");
  const types = await api("/api/job-types");
  $("job-type").innerHTML = Object.entries(types)
    .map(([k, v]) => `<option value="${esc(k)}" data-needs-config="${v.needs_config}">${esc(v.title)} (${esc(k)})</option>`)
    .join("");
  if (!$("job-config").value) {
    $("job-config").value = (await api("/api/config-template")).yaml;
  }
  syncConfigVisibility();
};
function syncConfigVisibility() {
  const sel = $("job-type").selectedOptions[0];
  $("job-config-label").style.display =
    sel && sel.dataset.needsConfig === "true" ? "" : "none";
}
$("job-type").onchange = syncConfigVisibility;
const closeModal = () => modal.classList.add("hidden");
$("job-modal-close").onclick = closeModal;
$("job-cancel").onclick = closeModal;
$("job-submit").onclick = async () => {
  const sel = $("job-type").selectedOptions[0];
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        type: $("job-type").value,
        title: $("job-title").value || undefined,
        config_yaml: sel.dataset.needsConfig === "true" ? $("job-config").value : undefined,
      }),
    });
    closeModal();
    pollJobs();
  } catch (err) {
    const box = $("job-error");
    box.textContent = err.message;
    box.classList.remove("hidden");
  }
};

/* =============================== boot =============================== */

async function refresh() {
  state.tiles = await api("/api/tiles");
  populateFilterOptions();
  syncFilterControls();
  renderTiles();
  await loadItems();
}

$("btn-refresh").onclick = () => { loadWorkspace(); refresh(); };
window.addEventListener("resize", () => { drawTimeline(); });

initMap();
bindFilterEvents();
bindPager();
bindTimeline();
loadWorkspace().catch((e) => {
  $("workspace-chips").innerHTML = `<span class="chip" style="color:var(--error)">${esc(e.message)}</span>`;
});
refresh().catch((e) => {
  $("items-count").textContent = e.message;
});
pollJobs();
