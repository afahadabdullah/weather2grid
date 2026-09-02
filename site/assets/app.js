/* Weather2Grid static dashboard. No CDN and no runtime server required. */
'use strict';

const S = {
  cycles: [], idx: 0, cycle: null, counties: [], geo: null,
  basemap: null, track: null, nhcTracks: [], outageStatus: null,
  byFips: new Map(), layer: 'peak_gust_ms', ratio: 0.15,
  triggered: new Set(), selected: null, playing: false, timer: null,
  loadToken: 0, curve: null, trackFrame: null,
  activeProvider: null,
  // extrapolation defaults OFF: with real HRRR cycles, the vast majority of
  // counties sit outside the model's synthetic training envelope (a data
  // fact, not a bug), so leaving this on by default hatches almost the whole
  // map and drowns out the risk-color read it's meant to sit on top of. The
  // toggle still exists for anyone who wants to see the coverage gap.
  overlays: { states: true, track: true, wind: true, extrapolation: false },
  view: 'event', zoom: 1, panX: 0, panY: 0, dragging: null,
  countiesGeoProjected: null, statesGeoProjected: null,
  geometryProjectionCache: new Map(), geometryKey: null, mapScreen: null,
};

const LAYERS = [
  { key: 'expected_customers_out', label: 'Expected out', fmt: 'int', ramp: 'impact' },
  { key: 'p90_customers_out', label: 'P90 out', fmt: 'int', ramp: 'impact' },
  { key: 'expected_outage_fraction', label: 'Outage fraction', fmt: 'pct', ramp: 'impact', fixed: [0, .35] },
  { key: 'prob_outage_fraction_gt_05', label: 'P(>5%)', fmt: 'pct', ramp: 'prob', fixed: [0, 1] },
  { key: 'peak_gust_ms', label: 'Peak gust', fmt: 'ms', ramp: 'gust' },
  { key: 'weather_spread_pp', label: 'Weather spread', fmt: 'pp', ramp: 'uncertainty' },
];

// Forecast sources this dashboard knows how to name. The stack in the rail is
// built from the cycles actually present in data/cycles.json (keyed off each
// cycle's hazard_source), never from a hardcoded list - a hardcoded list goes
// stale the moment an adapter ships, which is exactly how "WeatherNext 2 -
// adapter planned" ended up sitting above three live WeatherNext 2 cycles.
const PROVIDERS = [
  { id: 'hrrr', label: 'NOAA HRRR via AWS Open Data', match: (s) => s.startsWith('hrrr') },
  { id: 'weathernext2', label: 'Google DeepMind WeatherNext 2', match: (s) => s.startsWith('weathernext') },
  { id: 'gfs', label: 'NOAA GFS / GEFS', match: (s) => s.startsWith('gfs') || s.startsWith('gefs') },
];

function providerFor(hazardSource) {
  const source = String(hazardSource || '').toLowerCase();
  const known = PROVIDERS.find((provider) => provider.match(source));
  if (known) return known;
  // An unrecognised source is still shown, under its own raw name, rather
  // than being silently dropped out of the stack.
  return { id: `other:${source}`, label: source || 'unknown source', match: () => false };
}

function frameStartMs(cycle) {
  const valid = cycle.valid_start_utc || (cycle.meta && cycle.meta.valid_start_utc);
  if (valid) return Date.parse(valid) || 0;
  const issued = Date.parse(cycle.issued_utc || cycle.forecast_init_time_utc) || 0;
  return issued + Number(cycle.lead_hours || 0) * 36e5;
}

function providerFrameIndices(providerId = S.activeProvider) {
  const matches = S.cycles.map((cycle, index) => ({ cycle, index }))
    .filter(({ cycle }) => providerFor(cycle.hazard_source).id === providerId);
  if (!matches.length) return [];
  const latestInit = Math.max(...matches.map(({ cycle }) => Date.parse(cycle.issued_utc) || 0));
  return matches
    .filter(({ cycle }) => (Date.parse(cycle.issued_utc) || 0) === latestInit)
    .sort((a, b) => frameStartMs(a.cycle) - frameStartMs(b.cycle))
    .map(({ index }) => index);
}

function providerCoverage(providerId) {
  const frames = providerFrameIndices(providerId).map((index) => S.cycles[index]);
  if (!frames.length) return 'not in archive';
  const inputs = frames.reduce((sum, cycle) => sum
    + (Array.isArray(cycle.input_lead_hours) ? cycle.input_lead_hours.length : 0), 0);
  if (providerId === 'weathernext2') {
    return `latest initialization · ${inputs || 'unknown'} model leads · ${frames.length} map windows`;
  }
  const trackFrames = providerId === S.activeProvider && S.track && Array.isArray(S.track.points)
    ? S.track.points.length : null;
  return `latest initialization · ${inputs || 'unknown'} model leads${trackFrames ? ` · ${trackFrames} track frames` : ''}`;
}

// WeatherNext 2 publishes 100 m sustained wind, which the adapter carries in
// the product's peak_gust_ms column as an explicitly unvalidated gust proxy.
// Calling that "peak gust" in the UI would overstate it, so the wind label
// follows the cycle's own hazard_source.
function windLabel(short) {
  const source = String((S.cycle && S.cycle.meta && S.cycle.meta.hazard_source) || '');
  if (!source.includes('proxy')) return short ? 'Peak gust' : 'Peak modeled gust';
  return short ? '100 m wind*' : 'Peak 100 m wind (gust proxy)';
}
function layerLabel(layer) { return layer.key === 'peak_gust_ms' ? windLabel(true) : layer.label; }

const RAMPS = {
  impact: ['#0f2231', '#184f68', '#df6c3a', '#fba72c', '#ffe9a0'],
  prob: ['#0f2334', '#2c4b8e', '#7952c4', '#c968e0', '#f3d9ff'],
  gust: ['#0d2230', '#155d78', '#22a2aa', '#5ce6c4', '#f0fdf7'],
  uncertainty: ['#0f2232', '#2f497a', '#7462bd', '#bb86e0', '#f5dbff'],
};

const $ = (id) => document.getElementById(id);
const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
const integer = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const num = (v, d = 0) => v == null || Number.isNaN(v) ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const fmt = (v, kind) => v == null ? '—'
  : kind === 'pct' ? `${(v * 100).toFixed(v < .1 ? 1 : 0)}%`
  : kind === 'ms' ? `${Number(v).toFixed(0)} m/s`
  : kind === 'pp' ? `${Number(v).toFixed(1)} pp`
  : integer.format(v);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const lerp = (a, b, t) => a + (b - a) * t;

function hex2rgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}
function rampColor(stops, t) {
  t = Math.max(0, Math.min(1, t));
  const n = stops.length - 1, i = Math.min(Math.floor(t * n), n - 1), u = t * n - i;
  const a = hex2rgb(stops[i]), b = hex2rgb(stops[i + 1]);
  return `rgb(${Math.round(lerp(a[0], b[0], u))},${Math.round(lerp(a[1], b[1], u))},${Math.round(lerp(a[2], b[2], u))})`;
}

function albers(lon0 = -96, lat0 = 37.5, lat1 = 29.5, lat2 = 45.5) {
  const R = Math.PI / 180;
  const n = .5 * (Math.sin(lat1 * R) + Math.sin(lat2 * R));
  const C = Math.cos(lat1 * R) ** 2 + 2 * n * Math.sin(lat1 * R);
  const r0 = Math.sqrt(C - 2 * n * Math.sin(lat0 * R)) / n;
  return (lon, lat) => {
    const theta = n * ((lon - lon0) * R);
    const r = Math.sqrt(C - 2 * n * Math.sin(lat * R)) / n;
    // SVG's Y axis points down; negate projected northing so north stays up.
    return [r * Math.sin(theta), -(r0 - r * Math.cos(theta))];
  };
}

async function j(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} → ${response.status}`);
  return response.json();
}

const JSON_CACHE = new Map();
function cachedJson(url) {
  if (!JSON_CACHE.has(url)) {
    const request = j(url).catch((error) => {
      JSON_CACHE.delete(url);
      throw error;
    });
    JSON_CACHE.set(url, request);
  }
  return JSON_CACHE.get(url);
}

function decodeCountyData(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || payload.format !== 'w2g-columnar-v1'
      || !Array.isArray(payload.columns) || !Array.isArray(payload.rows)) {
    throw new Error('Unsupported county data format');
  }
  return payload.rows.map((values) => {
    const row = {};
    for (let index = 0; index < payload.columns.length; index += 1) {
      row[payload.columns[index]] = values[index];
    }
    return row;
  });
}

async function boot() {
  const status = await cachedJson('data/status.json');
  showBanner(status.banner);
  const [cycles, basemap, nhc, outageStatus] = await Promise.all([
    cachedJson('data/cycles.json'), cachedJson('data/basemap.geojson'),
    cachedJson('data/nhc-active-tracks.json').catch(() => null),
    cachedJson('data/live-outage-status.json').catch(() => null),
  ]);
  S.cycles = cycles; S.basemap = basemap;
  S.nhcTracks = nhc && nhc.available && Array.isArray(nhc.tracks) ? nhc.tracks : [];
  S.outageStatus = outageStatus;
  if (!S.cycles.length) { $('event-name').textContent = 'No forecast products found'; return; }
  S.cycles.sort((a, b) => frameStartMs(a) - frameStartMs(b));
  const initialIndex = S.cycles.reduce((best, cycle, index) => {
    const issued = Date.parse(cycle.issued_utc) || 0;
    const bestIssued = Date.parse(S.cycles[best].issued_utc) || 0;
    return issued > bestIssued || (issued === bestIssued && Number(cycle.lead_hours || 0) < Number(S.cycles[best].lead_hours || 0)) ? index : best;
  }, 0);
  S.activeProvider = providerFor(S.cycles[initialIndex].hazard_source).id;
  const slider = $('cycle');
  slider.addEventListener('input', () => {
    stopPlayback();
    const frames = activeFrames();
    if (frames[+slider.value]) selectFrame(frames[+slider.value]);
  });
  $('cycle-prev').addEventListener('click', () => { stopPlayback(); stepCycle(-1); });
  $('cycle-next').addEventListener('click', () => { stopPlayback(); stepCycle(1); });
  $('cycle-play').addEventListener('click', togglePlayback);
  $('playback-speed').addEventListener('change', () => { if (S.playing) { stopPlayback(); startPlayback(); } });
  $('d-close').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.code === 'Space' && !/INPUT|SELECT|BUTTON|TEXTAREA/.test(event.target.tagName)) {
      event.preventDefault(); togglePlayback();
    }
    if (event.key === 'Escape') closeDrawer();
  });
  buildLayers(); wireRatio(); wireOverlays(); wireMapControls();
  await loadCycle(initialIndex);
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { drawMap(); drawCurve(); drawCdfIfOpen(); }, 100);
  });
}

function showBanner(banner) {
  const host = $('banner');
  host.className = `banner ${banner.level}`;
  const title = document.createElement('b'), detail = document.createElement('span');
  title.textContent = banner.title; detail.textContent = banner.detail;
  host.replaceChildren(title, detail); host.hidden = false;
}

function buildCycleDots() {
  const frames = activeFrames(), position = activeFramePosition(frames);
  $('cycle-dots').innerHTML = frames.map((frame, i) => {
    const c = S.cycles[frame.cycleIndex];
    const prov = providerFor(c.hazard_source);
    const short = prov.id === 'hrrr' ? 'HRRR' : prov.id === 'weathernext2' ? 'WN2' : 'Forecast';
    const lead = frame.trackIndex != null ? S.track.points[frame.trackIndex].lead_hours : c.lead_hours;
    const desc = `${short}: ${c.event_name || c.cycle_id} · lead +${lead || 0}h`;
    return `<i class="${i === position ? 'active' : ''}" title="${esc(desc)}"></i>`;
  }).join('');
}

function activeFrames() {
  const cycles = providerFrameIndices();
  if (cycles.length === 1 && cycles[0] === S.idx && S.track
      && Array.isArray(S.track.points) && S.track.points.length > 1) {
    return S.track.points.map((point, trackIndex) => ({
      cycleIndex: S.idx, trackIndex, lead_hours: point.lead_hours, valid_utc: point.valid_utc,
    }));
  }
  return cycles.map((cycleIndex) => ({ cycleIndex, trackIndex: null }));
}

function activeFramePosition(frames = activeFrames()) {
  const position = frames.findIndex((frame) => frame.cycleIndex === S.idx
    && (frame.trackIndex == null || frame.trackIndex === S.trackFrame));
  return Math.max(0, position);
}

async function selectFrame(frame) {
  if (!frame) return;
  if (frame.cycleIndex !== S.idx) {
    await loadCycle(frame.cycleIndex, frame.trackIndex);
    return;
  }
  S.trackFrame = frame.trackIndex;
  updateFrameNavigation(); updateCycleChrome(); drawForecast(); redrawStormOnly();
}

function updateFrameNavigation() {
  const frames = activeFrames();
  const position = activeFramePosition(frames);
  $('cycle').max = String(Math.max(0, frames.length - 1));
  $('cycle').value = String(position);
  $('cycle-prev').disabled = position === 0;
  $('cycle-next').disabled = position >= frames.length - 1;
  buildCycleDots(); drawSourceSwitch();
}

async function loadCycle(index, requestedTrackFrame = null) {
  const i = Math.max(0, Math.min(S.cycles.length - 1, index));
  const token = ++S.loadToken;
  const summary = S.cycles[i];
  const root = `data/cycles/${encodeURIComponent(summary.cycle_id)}`;
  const geometryUrl = summary.geometry_path
    ? `data/${summary.geometry_path}` : `${root}/counties.geojson`;
  const [cycle, counties, geo, track] = await Promise.all([
    cachedJson(`${root}/cycle.json`),
    cachedJson(`${root}/counties.json`),
    cachedJson(geometryUrl),
    cachedJson(`${root}/track.json`),
  ]);
  if (token !== S.loadToken) return;
  S.idx = i; S.cycle = cycle; S.counties = decodeCountyData(counties); S.geo = geo;
  S.activeProvider = providerFor(summary.hazard_source).id;
  S.geometryKey = geometryUrl;
  S.countiesGeoProjected = S.geometryProjectionCache.get(geometryUrl) || null;
  S.track = track && track.available !== false ? track : null;
  S.trackFrame = S.track ? Math.max(0, Math.min(
    S.track.points.length - 1,
    requestedTrackFrame == null ? Number(S.track.current_index || 0) : requestedTrackFrame,
  )) : null;
  S.curve = null;
  S.byFips = new Map(S.counties.map((row) => [String(row.county_fips), row]));
  updateFrameNavigation(); updateCycleChrome(); buildLayers();
  drawProvenance(); drawSplit(); drawPriority(); drawForecast(); drawSourceStack(); drawTail();
  await refreshTriggered();
  if (token !== S.loadToken) return;
  drawKpis(); drawMap(); await drawCurve();
  scheduleCyclePrefetch();
  if (S.selected && S.byFips.has(S.selected)) openDrawer(S.selected);
  else if (S.selected) closeDrawer();
}

function scheduleCyclePrefetch() {
  const run = () => providerFrameIndices().filter((index) => index !== S.idx).forEach((index) => {
    const summary = S.cycles[index], root = `data/cycles/${encodeURIComponent(summary.cycle_id)}`;
    const geometryUrl = summary.geometry_path
      ? `data/${summary.geometry_path}` : `${root}/counties.geojson`;
    [`${root}/cycle.json`, `${root}/counties.json`, geometryUrl, `${root}/track.json`]
      .forEach((url) => { cachedJson(url).catch(() => {}); });
  });
  if ('requestIdleCallback' in window) window.requestIdleCallback(run, { timeout: 1500 });
  else setTimeout(run, 50);
}

function formatCycleTime(issuedStr) {
  const date = new Date(issuedStr);
  if (Number.isNaN(+date)) return issuedStr;
  const utcHours = String(date.getUTCHours()).padStart(2, '0');
  const utcMins = String(date.getUTCMinutes()).padStart(2, '0');
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const utcDate = `${months[date.getUTCMonth()]} ${date.getUTCDate()}, ${utcHours}:${utcMins} UTC`;
  const localTime = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZoneName: 'short' });
  return `${utcDate} (${localTime})`;
}

function formatValidRange(cycle) {
  const meta = cycle.meta || {};
  const start = new Date(meta.valid_start_utc || new Date(new Date(cycle.issued_utc).getTime() + Number(cycle.lead_hours || 0) * 36e5));
  const end = meta.valid_end_utc ? new Date(meta.valid_end_utc) : null;
  if (Number.isNaN(+start)) return 'Valid time unavailable';
  const stamp = (date) => `${date.toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: 'UTC' })} ${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}Z`;
  return end && !Number.isNaN(+end) ? `Valid ${stamp(start)}–${stamp(end)}` : `Valid ${stamp(start)}`;
}

function formatCycleLead(cycle) {
  const meta = cycle.meta || {};
  const lead = cycle.lead_hours;
  let horizonText = '';
  if (meta.valid_start_utc && meta.valid_end_utc) {
    const start = new Date(meta.valid_start_utc);
    const end = new Date(meta.valid_end_utc);
    const spanHrs = Math.round((end - start) / 36e5);
    if (spanHrs > 0) horizonText = ` · ${spanHrs}h window`;
  }
  return lead != null ? `Starts +${lead}h${horizonText}` : horizonText.replace(/^ · /, '');
}

function formatValidInstant(value) {
  const date = new Date(value);
  if (Number.isNaN(+date)) return 'Valid time unavailable';
  const day = date.toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: 'UTC' });
  return `Valid ${day} ${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}Z`;
}

function updateCycleChrome() {
  const cycle = S.cycle, issued = new Date(cycle.issued_utc);
  const prov = providerFor(cycle.meta ? cycle.meta.hazard_source : cycle.hazard_source);
  const shortProv = prov.id === 'hrrr' ? 'NOAA HRRR' : prov.id === 'weathernext2' ? 'WeatherNext 2' : 'Forecast';
  const frames = activeFrames(), position = activeFramePosition(frames);
  const frame = frames[position], trackPoint = frame && frame.trackIndex != null && S.track
    ? S.track.points[frame.trackIndex] : null;
  const inputCount = Array.isArray(cycle.input_lead_hours) ? cycle.input_lead_hours.length
    : Array.isArray(cycle.meta && cycle.meta.input_lead_hours) ? cycle.meta.input_lead_hours.length : 0;
  $('event-name').textContent = cycle.event_name || cycle.event_id;
  $('overview-provider').textContent = `${shortProv} · ${providerCoverage(prov.id)}`;
  $('cycle-time').textContent = trackPoint ? formatValidInstant(trackPoint.valid_utc) : formatValidRange(cycle);
  $('cycle-init').textContent = `Initialized ${formatCycleTime(cycle.issued_utc)}`;
  $('cycle-counter').textContent = trackPoint
    ? `${shortProv} · track lead ${position + 1} of ${frames.length}`
    : `${shortProv} · map window ${position + 1} of ${frames.length}`;
  const leadEl = $('cycle-lead');
  leadEl.textContent = trackPoint
    ? `Lead +${trackPoint.lead_hours}h · storm-center frame`
    : `${formatCycleLead(cycle)} · aggregate of ${inputCount || 'unknown'} model leads`;
  const note = $('frame-note');
  if (note) note.textContent = trackPoint
    ? `Playback moves through ${frames.length} available track points. The county wind/risk layer remains the +${cycle.forecast_horizon_hours || 18}h aggregate.`
    : `This product exports ${inputCount || 'unknown'} input leads as one aggregate map; individual lead maps are not present in the JSON.`;
  if (cycle.meta && cycle.meta.valid_start_utc && cycle.meta.valid_end_utc) {
    leadEl.title = `Forecast valid from ${cycle.meta.valid_start_utc} to ${cycle.meta.valid_end_utc}`;
    $('cycle-time').title = `Forecast valid from ${cycle.meta.valid_start_utc} to ${cycle.meta.valid_end_utc}`;
  }
  const liveAgeHours = Number.isNaN(+issued) ? null : (Date.now() - issued.getTime()) / 36e5;
  const freshness = cycle.degraded_mode ? 'degraded' : liveAgeHours != null && liveAgeHours > 12 ? 'stale' : cycle.freshness;
  $('freshness').textContent = freshness; $('freshness').className = `pill ${freshness}`;
  $('source-badge').textContent = `${cycle.meta.forecast_provider || 'unknown'} · ${S.counties.length} counties`;
  [...$('cycle-dots').children].forEach((dot, i) => dot.classList.toggle('active', i === position));
  document.querySelectorAll('[data-overlay="track"],[data-overlay="wind"],[data-view="storm"]').forEach((button) => {
    button.disabled = !S.track;
    button.title = S.track ? ''
      : 'No cyclone track in this product. Area wind outlooks carry a county wind field instead \u2014 see the wind map layer.';
  });
  const hasBasemap = Boolean(S.basemap && S.basemap.features && S.basemap.features.length);
  document.querySelectorAll('[data-overlay="states"],[data-view="conus"]').forEach((button) => {
    button.disabled = !hasBasemap; button.title = hasBasemap ? '' : 'No CONUS basemap in this archive';
  });
}

function stepCycle(delta) {
  const frames = activeFrames(), position = activeFramePosition(frames);
  const target = Math.max(0, Math.min(frames.length - 1, position + delta));
  if (frames[target]) selectFrame(frames[target]);
}
function togglePlayback() { S.playing ? stopPlayback() : startPlayback(); }
function startPlayback() {
  const frames = activeFrames();
  if (frames.length < 2) return;
  let position = activeFramePosition(frames);
  if (position >= frames.length - 1) { position = 0; selectFrame(frames[0]); }
  S.playing = true; updatePlayButton();
  S.timer = setInterval(async () => {
    const framesNow = activeFrames(), activePosition = activeFramePosition(framesNow);
    if (activePosition >= framesNow.length - 1) { stopPlayback(); return; }
    await selectFrame(framesNow[activePosition + 1]);
  }, +$('playback-speed').value);
}
function stopPlayback() { clearInterval(S.timer); S.timer = null; S.playing = false; updatePlayButton(); }
function updatePlayButton() {
  const button = $('cycle-play');
  button.setAttribute('aria-pressed', String(S.playing));
  button.setAttribute('aria-label', S.playing ? 'Pause forecast frames' : 'Play forecast frames');
  button.querySelector('span').textContent = S.playing ? 'Ⅱ' : '▶';
  button.querySelector('b').textContent = S.playing ? 'Pause' : 'Play';
}

function buildLayers() {
  const host = $('layers'); host.innerHTML = '';
  LAYERS.forEach((layer) => {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = layerLabel(layer); button.setAttribute('role', 'tab');
    if (layer.key === 'peak_gust_ms') button.title = windLabel(false);
    button.setAttribute('aria-selected', String(layer.key === S.layer));
    button.addEventListener('click', () => {
      S.layer = layer.key;
      [...host.children].forEach((child) => child.setAttribute('aria-selected', String(child === button)));
      updateCountyLayer();
    });
    host.appendChild(button);
  });
}
function wireOverlays() {
  document.querySelectorAll('[data-overlay]').forEach((button) => button.addEventListener('click', () => {
    const key = button.dataset.overlay; S.overlays[key] = !S.overlays[key];
    button.setAttribute('aria-pressed', String(S.overlays[key])); drawMap();
  }));
}
function wireMapControls() {
  document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => {
    S.view = button.dataset.view; resetViewport();
    document.querySelectorAll('[data-view]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
    drawMap();
  }));
  $('zoom-in').addEventListener('click', () => zoomMap(1.35));
  $('zoom-out').addEventListener('click', () => zoomMap(1 / 1.35));
  $('zoom-reset').addEventListener('click', () => { resetViewport(); drawMap(); });
  const svg = $('map');
  svg.addEventListener('wheel', (event) => {
    event.preventDefault();
    const box = svg.getBoundingClientRect();
    zoomMap(event.deltaY < 0 ? 1.16 : 1 / 1.16, event.clientX - box.left, event.clientY - box.top);
  }, { passive: false });
  svg.addEventListener('pointerdown', (event) => {
    if (event.target.classList.contains('county')) return;
    S.dragging = { x: event.clientX, y: event.clientY, panX: S.panX, panY: S.panY };
    svg.setPointerCapture(event.pointerId); svg.classList.add('dragging');
  });
  svg.addEventListener('pointermove', (event) => {
    if (!S.dragging) return;
    S.panX = S.dragging.panX + event.clientX - S.dragging.x;
    S.panY = S.dragging.panY + event.clientY - S.dragging.y;
    clampViewport(); applyViewport();
  });
  const endDrag = () => { S.dragging = null; svg.classList.remove('dragging'); };
  svg.addEventListener('pointerup', endDrag); svg.addEventListener('pointercancel', endDrag);
}
function resetViewport() { S.zoom = 1; S.panX = 0; S.panY = 0; }
function zoomMap(factor, cx = ($('map').clientWidth || 900) / 2, cy = ($('map').clientHeight || 560) / 2) {
  const old = S.zoom, next = Math.max(1, Math.min(8, old * factor));
  S.panX = cx - (cx - S.panX) * next / old;
  S.panY = cy - (cy - S.panY) * next / old;
  S.zoom = next; clampViewport(); applyViewport();
}
function clampViewport() {
  const W = $('map').clientWidth || 900, H = $('map').clientHeight || 560;
  S.panX = Math.min(0, Math.max(W * (1 - S.zoom), S.panX));
  S.panY = Math.min(0, Math.max(H * (1 - S.zoom), S.panY));
}
function focusMapPoint(x, y) {
  const old = S.zoom, next = Math.max(2.4, old);
  const baseX = (x - S.panX) / old, baseY = (y - S.panY) / old;
  S.zoom = next;
  S.panX = ($('map').clientWidth || 900) / 2 - baseX * next;
  S.panY = ($('map').clientHeight || 560) / 2 - baseY * next;
  clampViewport(); applyViewport();
}
function applyViewport() {
  const group = $('map').querySelector('.map-viewport');
  if (group) group.setAttribute('transform', `translate(${S.panX} ${S.panY}) scale(${S.zoom})`);
  updateScaleBar();
}
function activeLayer() { return LAYERS.find((layer) => layer.key === S.layer) || LAYERS[0]; }
function layerDomain(layer) {
  const values = S.counties.map((row) => row[layer.key]).filter((v) => v != null && !Number.isNaN(v));
  const maxVal = values.length ? Math.max(...values) : 1;
  if (layer.key === 'expected_outage_fraction') {
    const cap = Math.min(0.35, Math.max(0.10, Math.ceil(maxVal * 20) / 20));
    return [0, cap];
  }
  if (layer.fixed) return layer.fixed;
  return values.length ? [0, maxVal || 1] : [0, 1];
}

function stormCategory(vmaxKt) {
  if (vmaxKt < 64) return null;
  if (vmaxKt < 83) return 1; if (vmaxKt < 96) return 2;
  if (vmaxKt < 113) return 3; if (vmaxKt < 137) return 4; return 5;
}
function stormMeta(trackSource = S.track, selectedFrame = S.trackFrame) {
  if (!trackSource || !Array.isArray(trackSource.points) || !trackSource.points.length) return null;
  const currentIndex = Math.max(0, Math.min(
    trackSource.points.length - 1,
    selectedFrame == null ? Number(trackSource.current_index || 0) : selectedFrame,
  ));
  const current = trackSource.points[currentIndex];
  const coneByLead = trackSource.cone_radius_nm_by_lead || {};
  const points = trackSource.points.map((point, i) => {
    const lead = point.lead_hours != null ? point.lead_hours : (i - currentIndex) * 6;
    const coneNm = point.cone_radius_nm != null ? point.cone_radius_nm
      : coneByLead[String(lead)] != null ? coneByLead[String(lead)]
      : lead >= 0 ? 12 + 1.7 * lead + .013 * lead ** 2 : 0;
    return {
      lat: point.lat, lon: point.lon, lead_hours: lead,
      valid_time_utc: point.valid_utc, max_wind_ms: point.vmax_kt * .514444,
      pressure_hpa: point.pmin_mb, uncertainty_km: coneNm * 1.852,
      raw: point, selected: i === currentIndex,
    };
  });
  const classification = trackSource.classification || current.stage || '';
  const isCyclone = /tropical|cyclone|hurricane|typhoon|depression/i.test(classification)
    || /nhc|atcf/i.test(String(trackSource.source || ''));
  return {
    classification,
    isCyclone,
    category: isCyclone ? stormCategory(current.vmax_kt) : null, center_lat: current.lat,
    center_lon: current.lon, max_wind_ms: current.vmax_kt * .514444,
    max_wind_kt: current.vmax_kt, min_pressure_hpa: current.pmin_mb,
    wind_radii_km: { '34kt': current.r34_nm * 1.852, '50kt': current.r50_nm * 1.852, '64kt': current.r64_nm * 1.852 },
    currentIndex, track: points, stormId: trackSource.storm_id || trackSource.name || '',
    windRadiiGeojson: trackSource.wind_radii_geojson || null,
  };
}

function drawMap() {
  const svg = $('map');
  if (!S.geo) return;
  $('map-title').textContent = S.layer === 'peak_gust_ms' ? 'County wind field' : 'County outage risk';
  const W = svg.clientWidth || 900, H = svg.clientHeight || 560, proj = albers();
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const projectCollection = (collection, key) => {
    const bounds = [Infinity, Infinity, -Infinity, -Infinity], paths = [];
    for (const feature of (collection && collection.features) || []) {
      const polys = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.coordinates;
      const parts = [];
      for (const poly of polys) for (const ring of poly) {
        let d = '';
        ring.forEach((point, i) => {
          const [x, y] = proj(point[0], point[1]);
          bounds[0] = Math.min(bounds[0], x); bounds[1] = Math.min(bounds[1], y);
          bounds[2] = Math.max(bounds[2], x); bounds[3] = Math.max(bounds[3], y);
          d += `${i ? 'L' : 'M'}${x.toFixed(6)} ${y.toFixed(6)}`;
        });
        parts.push(`${d}Z`);
      }
      paths.push({ id: String(feature.properties[key] || feature.id || ''), d: parts.join(' ') });
    }
    return { bounds, paths };
  };
  if (!S.countiesGeoProjected) {
    S.countiesGeoProjected = projectCollection(S.geo, 'county_fips');
    S.geometryProjectionCache.set(S.geometryKey, S.countiesGeoProjected);
  }
  if (!S.statesGeoProjected) S.statesGeoProjected = projectCollection(S.basemap, 'state');
  const countiesGeo = S.countiesGeoProjected, statesGeo = S.statesGeoProjected;
  const storm = stormMeta(), track = storm && Array.isArray(storm.track) ? storm.track : [];
  const officialStorms = S.nhcTracks.map((item) => stormMeta(item, null)).filter(Boolean)
    .filter((item) => !storm || item.stormId !== storm.stormId);
  const visibleStorms = [...(storm ? [storm] : []), ...officialStorms];
  const visibleTrack = visibleStorms.flatMap((item) => item.track);
  let bounds;
  if (S.view === 'storm' && visibleTrack.length) {
    bounds = [Infinity, Infinity, -Infinity, -Infinity];
    visibleStorms.forEach((item) => item.track.forEach((point) => {
      const [x, y] = proj(point.lon, point.lat);
      const windKm = Math.max(point.uncertainty_km || 0, (item.wind_radii_km && item.wind_radii_km['34kt']) || 150);
      const radius = (windKm + 180) / 6371;
      bounds[0] = Math.min(bounds[0], x - radius); bounds[1] = Math.min(bounds[1], y - radius);
      bounds[2] = Math.max(bounds[2], x + radius); bounds[3] = Math.max(bounds[3], y + radius);
    }));
  } else if (S.view === 'conus' && statesGeo.paths.length) {
    bounds = [...statesGeo.bounds];
  } else {
    bounds = [...countiesGeo.bounds];
    if (track.length && S.overlays.track) {
      track.forEach((point) => {
        const [x, y] = proj(point.lon, point.lat), radius = Math.max(0, point.uncertainty_km || 0) / 6371;
        bounds[0] = Math.min(bounds[0], x - radius); bounds[1] = Math.min(bounds[1], y - radius);
        bounds[2] = Math.max(bounds[2], x + radius); bounds[3] = Math.max(bounds[3], y + radius);
      });
    }
  }
  let [x0, y0, x1, y1] = bounds;
  const pad = 28, sx = (W - pad * 2) / (x1 - x0 || 1), sy = (H - pad * 2) / (y1 - y0 || 1), scale = Math.min(sx, sy);
  S.mapScale = scale;
  const ox = pad + ((W - pad * 2) - (x1 - x0) * scale) / 2, oy = pad + ((H - pad * 2) - (y1 - y0) * scale) / 2;
  const screen = (lon, lat) => { const [x, y] = proj(lon, lat); return [ox + (x - x0) * scale, oy + (y - y0) * scale]; };
  S.mapScreen = screen;
  const layer = activeLayer(), [lo, hi] = layerDomain(layer), stops = RAMPS[layer.ramp];
  const hatchTile = 6 / scale;
  let out = `<defs><pattern id="hatch" width="${hatchTile}" height="${hatchTile}" patternTransform="rotate(45)" patternUnits="userSpaceOnUse"><line x1="0" y1="0" x2="0" y2="${hatchTile}" stroke="#d9edf1" stroke-width="${hatchTile / 5}" opacity=".22"/></pattern></defs>`;
  out += `<g class="map-viewport" transform="translate(${S.panX} ${S.panY}) scale(${S.zoom})">`;
  out += `<g transform="translate(${ox} ${oy}) scale(${scale}) translate(${-x0} ${-y0})">`;
  const hatchParts = [];
  for (const path of countiesGeo.paths) {
    const row = S.byFips.get(path.id), value = row ? row[layer.key] : null;
    const fill = value == null ? '#0f2231' : rampColor(stops, (value - lo) / ((hi - lo) || 1));
    const classes = ['county'];
    if (S.triggered.has(path.id)) classes.push('trig');
    if (S.selected === path.id) classes.push('sel');
    const title = row ? `${row.county_name}, ${row.state} — ${layerLabel(layer)}: ${fmt(value, layer.fmt)}` : path.id;
    out += `<path class="${classes.join(' ')}" d="${path.d}" fill="${fill}" vector-effect="non-scaling-stroke" data-fips="${esc(path.id)}" tabindex="0"><title>${esc(title)}</title></path>`;
    if (S.overlays.extrapolation && row && row.training_envelope_flag !== 'inside') hatchParts.push(path.d);
  }
  // don't consistently follow a single winding direction, so combining
  // ~2856 disjoint county subpaths into one <path> can, in principle, let
  // mismatched winding between neighboring counties overlap incorrectly
  // under nonzero fill. This did NOT turn out to be the cause of the
  // diagonal "shaded" wedge seen in production (verified by rendering both
  // fill-rules against real geometry - both looked identical and correct;
  // the actual cause was the pattern-tile scale bug fixed above via
  // `hatchTile`). Kept anyway since evenodd is the more correct choice for
  // a combined multi-ring path regardless, and costs nothing here.
  if (hatchParts.length) out += `<path d="${hatchParts.join(' ')}" fill="url(#hatch)" fill-rule="evenodd" pointer-events="none" stroke="none"/>`;
  out += '</g>';
  if (S.overlays.states && statesGeo.paths.length) {
    out += `<g class="state-layer" transform="translate(${ox} ${oy}) scale(${scale}) translate(${-x0} ${-y0})">`;
    statesGeo.paths.forEach((path) => { out += `<path class="state-shape" d="${path.d}" vector-effect="non-scaling-stroke"><title>${esc(path.id)}</title></path>`; });
    out += '</g>';
  }
  out += `<g id="storm-overlay">${visibleStorms.map((item) => drawStormOverlay(item.track, item, screen, scale)).join('')}</g>`;
  out += '</g>';
  svg.innerHTML = out;
  svg.querySelectorAll('.county').forEach((node) => {
    node.addEventListener('click', (event) => { focusMapPoint(event.offsetX, event.offsetY); openDrawer(node.dataset.fips); });
    node.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDrawer(node.dataset.fips); } });
  });
  drawLegend(layer, lo, hi, stops); updateScaleBar();
  const states = [...new Set(S.counties.map((row) => row.state))].sort();
  const viewLabel = S.view === 'storm' ? 'Storm view' : S.view === 'conus' ? 'CONUS view' : 'Event view';
  $('map-domain').textContent = `${viewLabel} · ${integer.format(S.counties.length)} counties`;
  $('map-domain').title = `${states.join(' + ')} county footprint${track.length ? ' with supplied storm track' : ''}${officialStorms.length ? ` · ${officialStorms.length} official NHC ocean track${officialStorms.length === 1 ? '' : 's'}` : ''}`;
}

function redrawStormOnly() {
  const host = $('storm-overlay'), storm = stormMeta();
  const officialStorms = S.nhcTracks.map((item) => stormMeta(item, null)).filter(Boolean)
    .filter((item) => !storm || item.stormId !== storm.stormId);
  const storms = [...(storm ? [storm] : []), ...officialStorms];
  if (!host || !S.mapScreen || !storms.length) { drawMap(); return; }
  host.innerHTML = storms.map((item) => drawStormOverlay(item.track, item, S.mapScreen, S.mapScale)).join('');
}

function updateCountyLayer() {
  const svg = $('map'), nodes = svg.querySelectorAll('.county');
  if (!nodes.length) { drawMap(); return; }
  const layer = activeLayer(), [lo, hi] = layerDomain(layer), stops = RAMPS[layer.ramp];
  $('map-title').textContent = S.layer === 'peak_gust_ms' ? 'County wind field' : 'County outage risk';
  nodes.forEach((node) => {
    const row = S.byFips.get(node.dataset.fips), value = row ? row[layer.key] : null;
    node.setAttribute('fill', value == null ? '#0f2231' : rampColor(stops, (value - lo) / ((hi - lo) || 1)));
    node.classList.toggle('trig', S.triggered.has(node.dataset.fips));
    node.classList.toggle('sel', S.selected === node.dataset.fips);
    const title = node.querySelector('title');
    if (title && row) {
      title.textContent = `${row.county_name}, ${row.state} — ${layerLabel(layer)}: ${fmt(value, layer.fmt)}`;
    }
  });
  drawLegend(layer, lo, hi, stops); updateScaleBar();
}

// Intensity tier for one track point, keyed off its max sustained wind
// (kt) - drives marker color/size everywhere in the storm overlay so a
// glance at dot color or ring color tells you category, the same way an
// NHC track map does, without needing to hover every point.
function stormTier(vmaxKt, isCyclone = true) {
  if (vmaxKt == null) return { cat: null, color: '#8ba0ac', label: 'Unknown intensity' };
  if (!isCyclone) return vmaxKt < 34
    ? { cat: null, color: '#3fd4e5', label: 'Surface low' }
    : { cat: null, color: '#ffb44c', label: 'Strong surface low' };
  if (vmaxKt < 34) return { cat: null, color: '#3fd4e5', label: 'Tropical depression' };
  if (vmaxKt < 64) return { cat: null, color: '#4fd1a1', label: 'Tropical storm' };
  const cat = stormCategory(vmaxKt);
  const color = { 1: '#ffb44c', 2: '#ff774d', 3: '#ff626d', 4: '#ff4f8a', 5: '#9a8cff' }[cat] || '#ff626d';
  return { cat, color, label: `Category ${cat}` };
}

// A compact modern cyclone mark: three rounded spiral arms, a crisp eye,
// and an intensity-colored perimeter. It remains legible over every risk
// ramp without resembling the older filled pinwheel symbol.
function stormGlyph(cx, cy, r, color, title) {
  const arm = `M0 0 C${(r * .12).toFixed(2)} ${(-r * .46).toFixed(2)} ${(r * .48).toFixed(2)} ${(-r * .78).toFixed(2)} ${(r * .94).toFixed(2)} ${(-r * .38).toFixed(2)}`;
  return `<g class="storm-glyph" transform="translate(${cx.toFixed(1)} ${cy.toFixed(1)})">` +
    `<title>${title}</title><circle class="storm-marker-bg" r="${(r * 1.12).toFixed(2)}" fill="#071018" stroke="${color}" stroke-width="1.5"/>` +
    [0, 120, 240].map((rotation) => `<path d="${arm}" fill="none" stroke="${color}" stroke-width="2.4" stroke-linecap="round" transform="rotate(${rotation})"/>`).join('') +
    `<circle r="${(r * .24).toFixed(2)}" fill="#f7feff" stroke="#071018" stroke-width="1.3"/></g>`;
}

function lowGlyph(cx, cy, r, color, title) {
  return `<g class="low-glyph" transform="translate(${cx.toFixed(1)} ${cy.toFixed(1)})"><title>${title}</title>` +
    `<circle r="${(r * 1.08).toFixed(2)}" fill="#071018" stroke="${color}" stroke-width="1.6"/>` +
    `<text y="${(r * .43).toFixed(2)}" text-anchor="middle" fill="${color}" font-size="${(r * 1.35).toFixed(2)}" font-weight="800">L</text></g>`;
}

// Color/dash per wind-radius tier, plus the compass angle its on-map label
// sits at (measured from the positive x-axis, screen y-down). The angles
// sit in the upper-left quadrant, well clear of the upper-right quadrant
// where each point's own "NOW"/"+Nh" label is drawn, so the two never
// collide, and are staggered from each other so the three read as a stack.
const WIND_TIER_STYLE = {
  '34kt': { color: '#3fd4e5', fill: 'rgba(63,212,229,.09)', dash: '2 3', angle: -160 },
  '50kt': { color: '#ffb44c', fill: 'rgba(255,180,76,.10)', dash: '6 3', angle: -135 },
  '64kt': { color: '#ff626d', fill: 'rgba(255,98,109,.12)', dash: 'none', angle: -110 },
};

function drawStormOverlay(track, storm, screen, scale) {
  const points = track.map((point) => ({ ...point, xy: screen(point.lon, point.lat) }));
  const selectedIndex = Math.max(0, points.findIndex((point) => point.selected));
  const past = points.slice(0, selectedIndex + 1);
  const future = points.slice(selectedIndex);
  let out = '';
  if (S.overlays.track && future.length > 1) {
    const left = [], right = [];
    future.forEach((point, i) => {
      const before = future[Math.max(0, i - 1)].xy, after = future[Math.min(future.length - 1, i + 1)].xy;
      const dx = after[0] - before[0], dy = after[1] - before[1], length = Math.hypot(dx, dy) || 1;
      const radius = Math.max(5, (point.uncertainty_km || (20 + i * 35)) / 6371 * scale);
      const nx = -dy / length, ny = dx / length;
      left.push([point.xy[0] + nx * radius, point.xy[1] + ny * radius]);
      right.push([point.xy[0] - nx * radius, point.xy[1] - ny * radius]);
    });
    const cone = [...left, ...right.reverse()];
    out += `<path class="storm-cone" d="${cone.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join('')}Z"><title>Forecast-center uncertainty cone. It does not show storm size or impact extent.</title></path>`;
    if (past.length > 1) out += `<path class="storm-track-past" d="${past.map((p, i) => `${i ? 'L' : 'M'}${p.xy[0].toFixed(1)} ${p.xy[1].toFixed(1)}`).join('')}"/>`;
    out += `<path class="storm-track" d="${future.map((p, i) => `${i ? 'L' : 'M'}${p.xy[0].toFixed(1)} ${p.xy[1].toFixed(1)}`).join('')}"/>`;
  }
  const current = points[selectedIndex] || points[0];
  if (S.overlays.wind && current) {
    const radii = storm.wind_radii_km || {};
    const swaths = ((storm.windRadiiGeojson && storm.windRadiiGeojson.features) || [])
      .filter((feature) => Number((feature.properties || {}).tau) === Number(current.lead_hours))
      .sort((a, b) => Number((a.properties || {}).radii) - Number((b.properties || {}).radii));
    // NHC supplies asymmetric polygons by quadrant.  When that source data
    // exists, draw the official swath itself instead of pretending the wind
    // field is a symmetric circle.  The scalar circles below are only the
    // fallback for a track contract that has no polygons.
    if (swaths.length) {
      swaths.forEach((feature) => {
        const properties = feature.properties || {}, threshold = `${properties.radii}kt`;
        const style = WIND_TIER_STYLE[threshold] || WIND_TIER_STYLE['34kt'];
        const geometry = feature.geometry || {};
        const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates || [];
        const path = polygons.map((polygon) => polygon.map((ring) => ring.map((coordinate, i) => {
          const xy = screen(coordinate[0], coordinate[1]);
          return `${i ? 'L' : 'M'}${xy[0].toFixed(1)} ${xy[1].toFixed(1)}`;
        }).join(' ') + 'Z').join(' ')).join(' ');
        out += `<path class="wind-radius" style="fill:${style.fill};stroke:${style.color}" stroke-dasharray="${style.dash}" d="${path}"><title>Official NHC ${esc(String(properties.radii))}-kt forecast wind swath</title></path>`;
      });
    }
    // Largest radius (34kt) drawn first, smallest (64kt) last, so each
    // tier's own color shows as the annulus outside the next tier in -
    // one glance reads how wind speed ramps up toward the center, instead
    // of three same-colored circles that only differed by radius before.
    if (!swaths.length) [['34kt', radii['34kt']], ['50kt', radii['50kt']], ['64kt', radii['64kt']]].forEach(([label, km]) => {
      if (!km) return;
      const style = WIND_TIER_STYLE[label], r = Math.max(4, km / 6371 * scale);
      out += `<circle class="wind-radius" style="fill:${style.fill};stroke:${style.color}" stroke-dasharray="${style.dash}" cx="${current.xy[0].toFixed(1)}" cy="${current.xy[1].toFixed(1)}" r="${r.toFixed(1)}"><title>${esc(label)} wind radius · ${km.toFixed(0)} km</title></circle>`;
      const rad = style.angle * Math.PI / 180;
      const lx = current.xy[0] + r * Math.cos(rad), ly = current.xy[1] + r * Math.sin(rad);
      out += `<text class="wind-radius-label" style="fill:${style.color}" x="${lx.toFixed(1)}" y="${ly.toFixed(1)}">${esc(label)}</text>`;
    });
  }
  if (S.overlays.track) points.forEach((point, i) => {
    const isCurrent = point.selected;
    const label = isCurrent ? `+${point.lead_hours}h` : point.lead_hours >= 0
      ? `+${point.lead_hours}h` : `${Math.abs(point.lead_hours)}h ago`;
    const isPast = i < selectedIndex;
    const tier = stormTier(point.raw && point.raw.vmax_kt != null ? point.raw.vmax_kt : null, storm.isCyclone);
    const tooltip = `${esc(label)} · ${esc(tier.label)} · ${fmt(point.max_wind_ms, 'ms')} · ${point.pressure_hpa || '—'} hPa`;
    if (isCurrent) {
      // A short heading arrow, pointed from the current fix toward the
      // next forecast point, so the storm's motion is visible without
      // reading the track line's slope - screen-space delta, not
      // geographic bearing, so it always agrees with the drawn track.
      const next = points[i + 1];
      if (next) {
        const dx = next.xy[0] - point.xy[0], dy = next.xy[1] - point.xy[1];
        const heading = Math.atan2(dx, -dy) * 180 / Math.PI, tip = 15, base = 6;
        out += `<g class="storm-heading" style="fill:${tier.color}" transform="translate(${point.xy[0].toFixed(1)} ${point.xy[1].toFixed(1)}) rotate(${heading.toFixed(1)})" pointer-events="none"><path d="M0 -${tip} L5 -${base} L-5 -${base} Z"/></g>`;
      }
      out += `<circle class="storm-halo" style="fill:${tier.color}" cx="${point.xy[0].toFixed(1)}" cy="${point.xy[1].toFixed(1)}" r="15" pointer-events="none"/>`;
      out += storm.isCyclone
        ? stormGlyph(point.xy[0], point.xy[1], 10, tier.color, tooltip)
        : lowGlyph(point.xy[0], point.xy[1], 10, tier.color, tooltip);
    } else {
      const r = isPast ? 3.2 : 3.6 + (tier.cat || 0) * 0.9;
      const fill = isPast ? '#64808f' : tier.color;
      out += `<circle class="track-point${isPast ? ' past' : ''}" style="fill:${fill}" cx="${point.xy[0].toFixed(1)}" cy="${point.xy[1].toFixed(1)}" r="${r.toFixed(1)}"><title>${tooltip}</title></circle>`;
    }
    if (point.lead_hours >= 0) out += `<text class="track-label" x="${(point.xy[0] + 10).toFixed(1)}" y="${(point.xy[1] - 9).toFixed(1)}">${esc(label)}</text>`;
  });
  return out;
}

function drawLegend(layer, lo, hi, stops) {
  $('legend').innerHTML = `<div class="lt">${esc(layerLabel(layer))}</div><div class="ramp" style="background:linear-gradient(90deg,${stops.join(',')})"></div><div class="ends"><span>${fmt(lo, layer.fmt)}</span><span>${fmt(hi, layer.fmt)}</span></div><div class="scale-bar"><i id="scale-line"></i><span id="scale-label">—</span></div>`;
}
function updateScaleBar() {
  const line = $('scale-line'), label = $('scale-label');
  if (!line || !label || !S.mapScale) return;
  const candidates = [10, 25, 50, 100, 200, 500, 1000, 2000];
  const pixels = (km) => km / 6371 * S.mapScale * S.zoom;
  const km = candidates.reduce((best, value) => Math.abs(pixels(value) - 70) < Math.abs(pixels(best) - 70) ? value : best, candidates[0]);
  line.style.width = `${Math.max(18, Math.min(110, pixels(km)))}px`;
  label.textContent = `${km.toLocaleString()} km`;
}

function wireRatio() {
  const input = $('ratio');
  S.ratio = Math.pow(10, +input.value); $('ratio-val').textContent = S.ratio.toFixed(2);
  input.addEventListener('input', async () => {
    S.ratio = Math.pow(10, +input.value);
    $('ratio-val').textContent = S.ratio.toFixed(S.ratio < .1 ? 3 : 2);
    await refreshTriggered(); drawKpis(); updateCountyLayer(); drawCurve();
  });
}
async function refreshTriggered() {
  if (!S.cycle) return;
  const counties = S.counties.filter((row) => Number(row.prob_outage_fraction_gt_05) > S.ratio);
  S.triggered = new Set(counties.map((row) => String(row.county_fips)));
  $('trig-count').textContent = counties.length;
  $('threshold-pct').textContent = `${Math.round(counties.length / Math.max(1, S.counties.length) * 100)}% of domain`;
}
async function drawCurve() {
  if (!S.cycle) return;
  const cycleId = S.cycle.cycle_id;
  if (!S.curve) {
    const curve = Array.from({ length: 60 }, (_, i) => {
      const ratio = Math.pow(10, Math.log10(.02) + i / 59 * (Math.log10(1) - Math.log10(.02)));
      return { cost_loss_ratio: ratio, counties_triggered: S.counties.filter((row) => Number(row.prob_outage_fraction_gt_05) > ratio).length };
    });
    S.curve = { threshold_field: 'prob_outage_fraction_gt_05', n_counties: S.counties.length, curve };
  }
  if (!S.cycle || S.cycle.cycle_id !== cycleId) return;
  const svg = $('clcurve'), W = svg.clientWidth || 290, H = 66, pad = 5;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const maxY = Math.max(...S.curve.curve.map((point) => point.counties_triggered), 1);
  const X = (ratio) => pad + (Math.log10(ratio) - Math.log10(.02)) / (Math.log10(1) - Math.log10(.02)) * (W - pad * 2);
  const Y = (count) => H - 12 - count / maxY * (H - 18);
  const points = S.curve.curve.map((point) => `${X(point.cost_loss_ratio).toFixed(1)},${Y(point.counties_triggered).toFixed(1)}`).join(' '), cursor = X(S.ratio);
  svg.innerHTML = `<defs><linearGradient id="curve-fill" x1="0" x2="1"><stop stop-color="#3fd4e5" stop-opacity=".35"/><stop offset="1" stop-color="#9a8cff" stop-opacity=".05"/></linearGradient></defs><polygon fill="url(#curve-fill)" points="${pad},${H - 12} ${points} ${W - pad},${H - 12}"/><polyline fill="none" stroke="#3fd4e5" stroke-width="1.8" points="${points}"/><line x1="${cursor}" y1="3" x2="${cursor}" y2="${H - 12}" stroke="#ff774d" stroke-width="1.3" stroke-dasharray="3 3"/><text x="${pad}" y="${H - 1}" font-size="8" fill="#718896">C/L 0.02</text><text x="${W - pad}" y="${H - 1}" font-size="8" fill="#718896" text-anchor="end">1.0</text>`;
}

function drawKpis() {
  const regional = S.cycle.meta.regional || {};
  $('kpi-expected').textContent = regional.expected == null ? compact.format(S.counties.reduce((sum, row) => sum + (row.expected_customers_out || 0), 0)) : compact.format(regional.expected);
  $('kpi-p90').textContent = regional.p90 == null ? '—' : compact.format(regional.p90);
  $('kpi-triggered').textContent = S.triggered.size;
  $('kpi-triggered-note').textContent = `counties above C/L ${S.ratio.toFixed(2)}`;
  const gust = Math.max(...S.counties.map((row) => row.peak_gust_ms || 0));
  $('kpi-gust').textContent = gust ? `${gust.toFixed(0)} m/s` : '—';
  $('kpi-gust-label').textContent = windLabel(false);
  const states = [...new Set(S.counties.map((row) => row.state))].sort();
  $('kpi-domain').textContent = `${integer.format(S.counties.length)} counties · ${states.length > 20 ? 'CONUS' : states.join(' + ')}`;
  $('kpi-domain').title = states.join(' + ');
}

function providerTargetIndex(providerId) {
  const indices = providerFrameIndices(providerId);
  if (!indices.length) return null;
  if (providerId === 'weathernext2') return indices[0];
  return indices.slice().sort((a, b) => {
    const A = S.cycles[a], B = S.cycles[b];
    const ta = Date.parse(A.issued_utc) || 0, tb = Date.parse(B.issued_utc) || 0;
    return ta === tb ? Number(A.lead_hours || 0) - Number(B.lead_hours || 0) : tb - ta;
  })[0];
}

function drawSourceSwitch() {
  const host = $('source-switch');
  if (!host) return;
  const present = [...new Set(S.cycles.map((cycle) => providerFor(cycle.hazard_source).id))];
  host.replaceChildren(...present.map((id) => {
    const known = PROVIDERS.find((provider) => provider.id === id);
    const sample = S.cycles[providerFrameIndices(id)[0]];
    const provider = known || providerFor(sample && sample.hazard_source);
    const button = document.createElement('button');
    button.type = 'button'; button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(id === S.activeProvider));
    const short = id === 'hrrr' ? 'NOAA HRRR' : id === 'weathernext2' ? 'WeatherNext 2' : provider.label;
    button.innerHTML = `<b>${esc(short)}</b><span>${esc(providerCoverage(id))}</span>`;
    button.title = `Show ${provider.label}`;
    button.addEventListener('click', () => {
      stopPlayback();
      const target = providerTargetIndex(id);
      if (target != null) loadCycle(target);
    });
    return button;
  }));
}

// One row per forecast source, built from the cycles actually in the archive.
// A source with cycles is a button that jumps to its newest initialisation at
// the shortest lead; a source with none is disabled and says so plainly,
// rather than claiming a roadmap this page cannot verify.
function drawSourceStack() {
  const host = $('source-stack');
  if (!host) return;
  const groups = new Map();
  S.cycles.forEach((summary, index) => {
    const provider = providerFor(summary.hazard_source);
    if (!groups.has(provider.id)) groups.set(provider.id, { label: provider.label, indices: [] });
    groups.get(provider.id).indices.push(index);
  });
  const activeId = providerFor((S.cycles[S.idx] || {}).hazard_source).id;
  const ids = [...new Set([...PROVIDERS.map((p) => p.id), ...groups.keys()])];
  const rows = ids.map((id) => {
    const group = groups.get(id);
    const known = PROVIDERS.find((provider) => provider.id === id);
    const label = group ? group.label : known ? known.label : id;
    const row = document.createElement('button');
    row.type = 'button';
    row.className = `source-row${!group ? ' planned' : id === activeId ? ' active' : ''}`;
    const name = document.createElement('span');
    name.append(document.createElement('i'));
    const bold = document.createElement('b');
    bold.textContent = label;
    name.append(bold);
    const state = document.createElement('em');
    if (!group) {
      state.textContent = 'not in archive';
      row.disabled = true;
      row.title = `${label} has no cycles in this archive.`;
    } else {
      state.textContent = id === activeId ? 'active' : providerCoverage(id);
      const target = providerTargetIndex(id);
      row.title = `Show ${label} · ${providerCoverage(id)}`;
      row.setAttribute('aria-pressed', String(id === activeId));
      row.addEventListener('click', () => { stopPlayback(); loadCycle(target); });
    }
    row.append(name, state);
    return row;
  });
  if (S.nhcTracks.length) {
    const row = document.createElement('button');
    row.type = 'button'; row.className = 'source-row active';
    row.title = 'Show official NHC ocean-storm tracks and wind swaths.';
    row.innerHTML = `<span><i></i><b>NOAA NHC ocean storms</b></span><em>${S.nhcTracks.length} active track${S.nhcTracks.length === 1 ? '' : 's'}</em>`;
    row.addEventListener('click', () => {
      S.view = 'storm';
      document.querySelectorAll('[data-view]').forEach((button) => {
        button.setAttribute('aria-pressed', String(button.dataset.view === 'storm'));
      });
      drawMap();
    });
    rows.push(row);
  }
  if (S.outageStatus && !S.outageStatus.available) {
    const row = document.createElement('button');
    row.type = 'button'; row.className = 'source-row planned'; row.disabled = true;
    row.title = S.outageStatus.reason || 'Observed outages are unavailable.';
    row.innerHTML = '<span><i></i><b>Observed outages</b></span><em>not configured</em>';
    rows.push(row);
  }
  host.replaceChildren(...rows);
}

function drawForecast() {
  const meta = S.cycle.meta, storm = stormMeta(), provider = meta.forecast_provider || meta.hazard_source || 'unknown';
  $('active-source').textContent = `${provider} · ${meta.hazard_model_version || 'forecast product'}`;
  $('provider-status').textContent = S.cycle.degraded_mode ? 'degraded' : (S.cycle.provider_status || 'ok');
  $('provider-status').className = `source-status${S.cycle.degraded_mode ? ' degraded' : ''}`;
  if (!storm) {
    $('storm-symbol').className = 'storm-symbol field';
    $('storm-class').textContent = meta.event_type === 'tropical_cyclone' ? 'Cyclone metadata pending' : 'Area wind outlook · no cyclone track';
    $('storm-name').textContent = S.cycle.event_name;
    // No track means no center, no category and no wind radii - but the
    // product still carries a county wind field, so show that here instead
    // of two em-dashes that read like the panel failed to load.
    const gusts = S.counties.map((row) => row.peak_gust_ms || 0);
    const peak = gusts.length ? Math.max(...gusts) : 0;
    const galeCount = gusts.filter((value) => value >= 17.5).length;
    $('storm-stats').innerHTML =
      `<div><b>${peak ? `${peak.toFixed(0)} m/s` : '—'}</b><span>${esc(windLabel(false))}</span></div>` +
      `<div><b>${integer.format(galeCount)}</b><span>Counties ≥ 34 kt</span></div>`;
    const chip = $('storm-chip'); chip.hidden = false;
    chip.innerHTML = '<b>No storm track in this JSON</b><span>Showing the exported county wind field only; no ocean cyclone center or wind radii were supplied.</span>';
    return;
  }
  $('storm-symbol').className = `storm-symbol${storm.isCyclone ? '' : ' low'}`;
  $('storm-class').textContent = `${storm.classification || 'Tropical cyclone'}${storm.category != null ? ` · Category ${storm.category}` : ''}`;
  $('storm-name').textContent = `${S.track.name || S.cycle.event_name} · ${S.track.storm_id || 'track supplied'}`;
  $('storm-stats').innerHTML = `<div><b>${Number(storm.center_lat).toFixed(1)}°, ${Math.abs(storm.center_lon).toFixed(1)}°W</b><span>Selected center</span></div><div><b>${storm.max_wind_kt || '—'} kt</b><span>Maximum wind</span></div><div><b>${storm.min_pressure_hpa || '—'} hPa</b><span>Minimum pressure</span></div><div><b>+${Math.max(...storm.track.map((point) => point.lead_hours))} h</b><span>Track horizon</span></div>`;
  const chip = $('storm-chip'); chip.hidden = false;
  chip.innerHTML = storm.isCyclone
    ? `<b>${esc(storm.classification || 'Cyclone')}${storm.category ? ` · Cat ${storm.category}` : ''} · ${esc(provider)}</b><span>Selected +${S.track.points[storm.currentIndex].lead_hours}h · ${storm.max_wind_kt || '—'} kt · ${storm.min_pressure_hpa || '—'} hPa</span>`
    : `<b>No ocean cyclone in this JSON</b><span>Experimental inland HRRR surface low · selected +${S.track.points[storm.currentIndex].lead_hours}h · zero supplied tropical wind radii.</span>`;
}

function drawTail() {
  const regional = S.cycle.meta.regional || {}, joint = regional.p90, independent = regional.p90_if_independent;
  $('tail-joint').querySelector('em').textContent = joint == null ? '—' : compact.format(joint);
  $('tail-independent').querySelector('em').textContent = independent == null ? '—' : compact.format(independent);
  const max = Math.max(joint || 1, independent || 1);
  $('tail-joint').style.setProperty('--w', `${Math.max(8, (joint || 0) / max * 100)}%`);
  $('tail-independent').style.setProperty('--w', `${Math.max(8, (independent || 0) / max * 100)}%`);
  if (regional.tail_understatement_ratio) $('tail-note').textContent = `Independent county summation understates this regional P90 by ${((regional.tail_understatement_ratio - 1) * 100).toFixed(0)}%.`;
}

function drawPriority() {
  const rows = [...S.counties].sort((a, b) => (b.expected_customers_out || 0) - (a.expected_customers_out || 0)).slice(0, 6);
  $('priority-table').innerHTML = rows.map((row) => `<tr data-fips="${esc(row.county_fips)}"><td>${esc(row.county_name)}<small>${esc(row.state)} · ${esc(row.county_fips)}</small></td><td>${num(row.expected_customers_out)}</td><td>${num(row.p90_customers_out)}</td><td>${fmt(row.prob_outage_fraction_gt_05, 'pct')}</td><td><span class="confidence ${row.product_confidence === 'reduced' ? 'reduced' : ''}">${esc(row.product_confidence || row.data_quality_flag || '—')}</span></td></tr>`).join('');
  $('priority-table').querySelectorAll('tr').forEach((row) => row.addEventListener('click', () => openDrawer(row.dataset.fips)));
}

function drawSplit() {
  const rows = [...S.counties].filter((row) => row.expected_outage_fraction != null).sort((a, b) => b.expected_outage_fraction - a.expected_outage_fraction).slice(0, 8);
  const max = Math.max(1e-6, ...rows.map((row) => Math.max(row.weather_spread_pp || 0, row.impact_spread_pp || 0)));
  $('split').innerHTML = rows.map((row) => {
    const weather = (row.weather_spread_pp || 0) / max * 100, impact = (row.impact_spread_pp || 0) / max * 100;
    return `<div class="srow"><span class="nm" title="${esc(row.county_name)}, ${esc(row.state)}">${esc(row.county_name)}</span><span class="bars"><i class="bar weather" style="width:${weather.toFixed(1)}%" title="Weather ${fmt(row.weather_spread_pp, 'pp')}"></i><i class="bar impact" style="width:${impact.toFixed(1)}%" title="Impact ${fmt(row.impact_spread_pp, 'pp')}"></i></span><span class="total">${fmt(Math.max(row.weather_spread_pp || 0, row.impact_spread_pp || 0), 'pp')}</span></div>`;
  }).join('');
}

function drawProvenance() {
  const meta = S.cycle;
  const m = meta.meta || {};
  const validSpan = m.valid_start_utc && m.valid_end_utc
    ? `${m.valid_start_utc.slice(5, 16).replace('T', ' ')}Z → ${m.valid_end_utc.slice(5, 16).replace('T', ' ')}Z`
    : null;
  const horizon = m.valid_start_utc && m.valid_end_utc
    ? `${Math.round((new Date(m.valid_end_utc) - new Date(m.valid_start_utc)) / 36e5)}h`
    : null;
  const rows = [
    ['Artifact', meta.model_artifact_id], ['Hazard', meta.hazard_source],
    ['Provider', m.forecast_provider || meta.provider_status],
    ['Lead / Window', meta.lead_hours != null ? `+${meta.lead_hours}h lead (${horizon || '—'} window)` : '—'],
    ['Valid period', validSpan || '—'],
    ['Training cutoff', (meta.training_data_cutoff_utc || '').slice(0, 10)],
    ['Release gate', meta.release_gate_passed ? 'passed' : 'NOT PASSED'], ['Synthetic', meta.synthetic ? 'YES' : 'no'],
    ['Schema', m.schema_version || '—'], ['Geography', m.geography_version || '—'],
  ];
  $('prov').innerHTML = rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value ?? '—')}</dd>`).join('');
}

async function openDrawer(fips) {
  const row = S.byFips.get(String(fips));
  if (!row) return;
  S.selected = String(fips);
  $('d-name').textContent = `${row.county_name}, ${row.state}`;
  $('d-sub').textContent = `FIPS ${row.county_fips} · ${num(row.customers_total)} customers`;
  const extrapolated = row.training_envelope_flag && row.training_envelope_flag !== 'inside';
  $('d-stats').innerHTML = stat('Expected out', num(row.expected_customers_out)) + stat('P90 out', num(row.p90_customers_out)) + stat('Expected fraction', fmt(row.expected_outage_fraction, 'pct')) + stat('P(>5%)', fmt(row.prob_outage_fraction_gt_05, 'pct')) + stat('Weather spread', fmt(row.weather_spread_pp, 'pp')) + stat('Impact spread', fmt(row.impact_spread_pp, 'pp')) + stat('Peak gust', fmt(row.peak_gust_ms, 'ms')) + stat('Envelope', row.training_envelope_flag, extrapolated);
  drawCdf(row);
  $('d-drivers').innerHTML = [['Damaging-wind hours', row.duration_hr != null ? Number(row.duration_hr).toFixed(1) : '—'], ['Hazard reference quality', row.hazard_reference_quality], ['Data quality', row.data_quality_flag], ['Product confidence', row.product_confidence]].map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value ?? '—')}</dd>`).join('');
  $('drawer').dataset.row = JSON.stringify(row); $('drawer').hidden = false; updateCountyLayer();
}
function stat(label, value, warn = false) { return `<div class="stat${warn ? ' warn' : ''}"><b>${esc(value ?? '—')}</b><small>${esc(label)}</small></div>`; }
function drawCdf(row) {
  const keys = Object.keys(row).filter((key) => /^q\d+_outage_fraction$/.test(key)).sort((a, b) => parseInt(a.slice(1)) - parseInt(b.slice(1)));
  const svg = $('d-cdf'); if (!keys.length) { svg.innerHTML = ''; return; }
  const points = keys.map((key) => ({ p: parseInt(key.slice(1)) / 100, v: row[key] || 0 }));
  const W = svg.clientWidth || 390, H = 130, pad = 24, maxV = Math.max(...points.map((point) => point.v), .01);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const X = (v) => pad + v / maxV * (W - pad * 2), Y = (p) => H - pad - p * (H - pad * 1.6);
  const line = points.map((point) => `${X(point.v).toFixed(1)},${Y(point.p).toFixed(1)}`).join(' ');
  svg.innerHTML = `<polyline fill="none" stroke="#3fd4e5" stroke-width="2" points="${line}"/>${points.map((point) => `<circle cx="${X(point.v)}" cy="${Y(point.p)}" r="3" fill="#3fd4e5"><title>P${point.p * 100} = ${(point.v * 100).toFixed(1)}%</title></circle>`).join('')}<line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="#29404e"/><text x="${pad}" y="${H - 7}" font-size="9" fill="#718896">0%</text><text x="${W - pad}" y="${H - 7}" font-size="9" fill="#718896" text-anchor="end">${(maxV * 100).toFixed(0)}% customers</text>`;
  $('d-cdfnote').textContent = 'Full predictive quantiles let each user apply their own operational threshold rather than relying on one headline probability.';
}
function drawCdfIfOpen() { if (!$('drawer').hidden && $('drawer').dataset.row) drawCdf(JSON.parse($('drawer').dataset.row)); }
function closeDrawer() { $('drawer').hidden = true; S.selected = null; updateCountyLayer(); }

boot().catch((error) => { $('event-name').textContent = `Dashboard failed to load: ${error.message}`; console.error(error); });
