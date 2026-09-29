"""
monet/dashboard.py
~~~~~~~~~~~~~~~~~~

Interactive web dashboard for the Monet calibration database.
Mounted at /dashboard by server.py (imported at the bottom of that file
to avoid circular-import issues).

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select

# Imported lazily inside each handler to avoid a circular-import at module
# load time (server.py imports this module at its very bottom).
import monet.server as _server  # noqa: E402
from monet.models import Calibration, Factor
from monet.serviceauth import require_scope

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# The dashboard's data routes are `read`-scoped: the browser sends the bearer
# token (from localStorage) that the page's JS collected at its login prompt.
# GET /dashboard/ (the HTML shell) stays public so the login UI can load; the
# data only comes back with a valid token. Edits go through the main API's
# write-scoped routes (e.g. POST /calibrations/delete), so a read token can
# view but not edit. On an auth-disabled loopback server require_scope returns
# None, so zero-config dev is unchanged.
_READ = [Depends(require_scope("read"))]


# ── Pydantic schema ────────────────────────────────────────────────────────────


class TimeseriesRequest(BaseModel):
    devices: Optional[List[str]] = None
    wavelengths: Optional[List[float]] = None
    laser_powers: Optional[List[float]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None


# ── API endpoints ──────────────────────────────────────────────────────────────


@router.get("/api/filters", dependencies=_READ)
def get_filters():
    """Return unique filter values and date range for sidebar population."""
    with _server._get_session() as session:
        rows = session.execute(select(Calibration)).scalars().all()

    if not rows:
        return {
            "devices": [],
            "wavelengths": [],
            "laser_powers": [],
            "date_min": None,
            "date_max": None,
        }

    devices = sorted(set(r.device_name for r in rows))
    wavelengths = sorted(set(r.wavelength_nm for r in rows))
    laser_powers = sorted(set(r.laser_power_mw for r in rows))
    dates = [r.calibration_date for r in rows]
    return {
        "devices": devices,
        "wavelengths": wavelengths,
        "laser_powers": laser_powers,
        "date_min": min(dates),
        "date_max": max(dates),
    }


@router.get("/api/transmission_objectives", dependencies=_READ)
def get_transmission_objectives(device: str = None):
    """Return all transmission_objective factor records, optionally filtered by device."""
    with _server._get_session() as session:
        stmt = select(Factor).order_by(
            Factor.device_name, Factor.wavelength_nm, Factor.calibration_date
        )
        if device:
            stmt = stmt.where(Factor.device_name == device)
        rows = session.execute(stmt).scalars().all()
    return [
        {
            "device": r.device_name,
            "wavelength": r.wavelength_nm,
            "date": r.calibration_date,
            "transmission_objective_mean": r.transmission_objective_mean,
            "transmission_objective_std": r.transmission_objective_std,
            "n_points": r.n_points,
        }
        for r in rows
    ]


@router.post("/api/timeseries", dependencies=_READ)
def get_timeseries(req: TimeseriesRequest):
    """Return filtered calibration records for the dashboard charts."""
    with _server._get_session() as session:
        stmt = select(Calibration).order_by(
            Calibration.device_name,
            Calibration.wavelength_nm,
            Calibration.laser_power_mw,
            Calibration.calibration_date,
            Calibration.calibration_time,
        )
        if req.devices:
            stmt = stmt.where(Calibration.device_name.in_(req.devices))
        if req.wavelengths:
            stmt = stmt.where(Calibration.wavelength_nm.in_(req.wavelengths))
        if req.laser_powers:
            stmt = stmt.where(Calibration.laser_power_mw.in_(req.laser_powers))
        if req.date_from:
            stmt = stmt.where(Calibration.calibration_date >= req.date_from)
        if req.date_to:
            stmt = stmt.where(Calibration.calibration_date <= req.date_to)

        rows = session.execute(stmt).scalars().all()

    records = []
    for r in rows:
        dt = f"{r.calibration_date}T{r.calibration_time}:00"
        records.append(
            {
                "device": r.device_name,
                "wavelength": r.wavelength_nm,
                "laser_power": r.laser_power_mw,
                "date": r.calibration_date,
                "time": r.calibration_time,
                "dt": dt,
                "parameters": json.loads(r.parameters_json),
            }
        )
    return {"records": records}


# ── HTML page ──────────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Monet Dashboard</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
  <style>
    body { overflow-x: hidden; font-size: 0.9rem; }
    #sidebar {
      width: 230px; min-width: 230px; height: 100vh;
      position: sticky; top: 0; overflow-y: auto;
      background: #f8f9fa; border-right: 1px solid #dee2e6;
      padding: 1rem; flex-shrink: 0;
    }
    #main { flex: 1; overflow-y: auto; padding: 1.25rem; min-width: 0; }
    select[multiple] { min-height: 88px; font-size: 0.8rem; }
    .tab-btn {
      cursor: pointer; border: none; background: none;
      padding: 0.45rem 1rem; border-bottom: 2px solid transparent;
      color: #555;
    }
    .tab-btn.active { border-bottom-color: #0d6efd; color: #0d6efd; font-weight: 600; }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; }
    .chart-card {
      background: #fff; border: 1px solid #dee2e6; border-radius: 6px;
      padding: 0.75rem; margin-bottom: 1rem;
    }
    #loading-overlay {
      display: none; position: fixed; top: 0; left: 0;
      width: 100%; height: 100%; background: rgba(255,255,255,0.78);
      z-index: 9999; align-items: center; justify-content: center;
    }
    #loading-overlay.show { display: flex; }
    .wl-dot {
      display: inline-block; width: 10px; height: 10px;
      border-radius: 50%; margin-right: 4px; vertical-align: middle;
    }
    .params-cell {
      max-width: 280px; overflow: hidden; text-overflow: ellipsis;
      white-space: nowrap; cursor: help; font-family: monospace; font-size: 0.75rem;
    }
    .plotly-chart { min-height: 160px; }
    #db-table-wrap table { font-size: 0.8rem; }
    #db-table-wrap tr.selected { background: #fff3cd !important; }
    #login-overlay {
      display: none; position: fixed; top: 0; left: 0;
      width: 100%; height: 100%; background: rgba(0,0,0,0.45);
      z-index: 10000; align-items: center; justify-content: center;
    }
    #login-card {
      background: #fff; border-radius: 8px; padding: 1.5rem;
      width: 360px; max-width: 92vw; box-shadow: 0 6px 24px rgba(0,0,0,0.2);
    }
    #login-error { color: #dc3545; min-height: 1.2em; }
    #auth-bar { font-size: 0.75rem; }
    button[disabled] { opacity: 0.5; cursor: not-allowed; }
  </style>
</head>
<body>

<!-- Loading overlay -->
<div id="loading-overlay">
  <div class="spinner-border text-primary" role="status">
    <span class="visually-hidden">Loading…</span>
  </div>
</div>

<!-- Login overlay (shown when the server requires a token) -->
<div id="login-overlay">
  <div id="login-card">
    <h6 class="fw-bold mb-2 text-primary">Monet Dashboard — sign in</h6>
    <p class="small text-muted mb-2">
      This server requires a token. Paste a monet token
      (a <code>read</code> token can view; a <code>write</code> token can also
      edit). Get one from the server admin (<code>monet token add</code>).
    </p>
    <input type="password" id="login-token" class="form-control form-control-sm mb-2"
           placeholder="paste token" onkeydown="if(event.key==='Enter')doLogin()">
    <div id="login-error" class="small mb-2"></div>
    <button class="btn btn-primary btn-sm w-100" onclick="doLogin()">Sign in</button>
  </div>
</div>

<div class="d-flex" style="height:100vh;">

  <!-- ── Sidebar ──────────────────────────────────────────────────────────── -->
  <div id="sidebar">
    <h6 class="fw-bold mb-1 text-primary">Monet Dashboard</h6>
    <div id="auth-bar" class="text-muted mb-2" style="display:none;"></div>

    <label class="form-label small fw-semibold mb-1">Microscopes</label>
    <div class="d-flex gap-1 mb-1">
      <button class="btn btn-outline-secondary btn-sm py-0 px-1"
              onclick="selectAll('sel-devices')">All</button>
      <button class="btn btn-outline-secondary btn-sm py-0 px-1"
              onclick="selectNone('sel-devices')">None</button>
    </div>
    <select id="sel-devices" multiple class="form-select mb-3"></select>

    <label class="form-label small fw-semibold mb-1">Wavelengths (nm)</label>
    <div class="d-flex gap-1 mb-1">
      <button class="btn btn-outline-secondary btn-sm py-0 px-1"
              onclick="selectAll('sel-wavelengths')">All</button>
      <button class="btn btn-outline-secondary btn-sm py-0 px-1"
              onclick="selectNone('sel-wavelengths')">None</button>
    </div>
    <select id="sel-wavelengths" multiple class="form-select mb-3"></select>

    <label class="form-label small fw-semibold mb-1">Laser Power (mW)</label>
    <div class="d-flex gap-1 mb-1">
      <button class="btn btn-outline-secondary btn-sm py-0 px-1"
              onclick="selectAll('sel-powers')">All</button>
      <button class="btn btn-outline-secondary btn-sm py-0 px-1"
              onclick="selectNone('sel-powers')">None</button>
    </div>
    <select id="sel-powers" multiple class="form-select mb-3"></select>

    <label class="form-label small fw-semibold mb-1">Date from</label>
    <input type="date" id="date-from" class="form-control form-control-sm mb-2">
    <label class="form-label small fw-semibold mb-1">Date to</label>
    <input type="date" id="date-to"   class="form-control form-control-sm mb-3">

    <button class="btn btn-primary btn-sm w-100 mb-3" onclick="update()">Update</button>

    <div class="small text-muted">
      <div>Microscopes: <span id="stat-devices"  class="fw-semibold text-dark">—</span></div>
      <div>Records:     <span id="stat-records"  class="fw-semibold text-dark">—</span></div>
    </div>
  </div>

  <!-- ── Main ─────────────────────────────────────────────────────────────── -->
  <div id="main">

    <!-- Tab bar -->
    <div class="border-bottom mb-3">
      <button class="tab-btn"        data-tab="param-history"
              onclick="switchTab('param-history')">Parameter History</button>
      <button class="tab-btn active" data-tab="power-range"
              onclick="switchTab('power-range')">Power History</button>
      <button class="tab-btn"        data-tab="latest-table"
              onclick="switchTab('latest-table')">Latest Calibrations</button>
      <button class="tab-btn"        data-tab="transmission"
              onclick="switchTab('transmission')">Objective Transmission</button>
      <button class="tab-btn"        data-tab="db-table"
              onclick="switchTab('db-table')">All Records</button>
    </div>

    <div id="tab-param-history" class="tab-pane">
      <div id="charts-param"></div>
    </div>
    <div id="tab-power-range"   class="tab-pane active">
      <div id="charts-power"></div>
    </div>
    <div id="tab-latest-table"  class="tab-pane">
      <div id="charts-latest"></div>
      <div id="table-latest"  class="mt-3"></div>
    </div>
    <div id="tab-transmission"  class="tab-pane">
      <div id="charts-transmission"></div>
    </div>
    <div id="tab-db-table" class="tab-pane">
      <div id="db-table-controls" class="d-flex align-items-center gap-2 mb-2 flex-wrap">
        <div class="form-check mb-0">
          <input class="form-check-input" type="checkbox" id="select-all-cb"
                 onchange="toggleSelectAll(this.checked)">
          <label class="form-check-label small" for="select-all-cb">Select all</label>
        </div>
        <button id="btn-del-selected" class="btn btn-sm btn-outline-danger"
                onclick="deleteSelected()">Delete selected</button>
        <button id="btn-del-all" class="btn btn-sm btn-danger"
                onclick="deleteAllInView()">Delete all in view</button>
        <span id="db-table-status" class="small text-muted ms-2"></span>
      </div>
      <div id="db-table-wrap" class="table-responsive"></div>
    </div>

  </div>
</div>

<script>
'use strict';

// ── Authentication (bearer token kept in localStorage) ────────────────────────
// The browser can't send a bearer header on navigation, so GET /dashboard/ is
// public and the token is collected here: read from localStorage, sent on every
// data request, and (re)prompted whenever the server answers 401. `read` views,
// `write` also edits. When the server has auth disabled (loopback dev), whoami
// reports authenticated=false and no login is required.
const TOKEN_KEY = 'monet_dashboard_token';
let AUTH = {
  token: localStorage.getItem(TOKEN_KEY) || '',
  scope: null, label: null, enforced: false,
};
let CAN_EDIT = true;  // recomputed by applyScope() once the scope is known

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (AUTH.token) h['Authorization'] = 'Bearer ' + AUTH.token;
  return h;
}

async function authedFetch(url, opts) {
  opts = opts || {};
  opts.headers = authHeaders(opts.headers);
  const res = await fetch(url, opts);
  if (res.status === 401) {
    showLogin('Your token was rejected. Paste a valid token to continue.');
    throw new Error('unauthorized');
  }
  return res;
}

function showLogin(msg) {
  document.getElementById('login-error').textContent = msg || '';
  document.getElementById('login-overlay').style.display = 'flex';
  const inp = document.getElementById('login-token');
  inp.value = '';
  inp.focus();
}
function hideLogin() {
  document.getElementById('login-overlay').style.display = 'none';
}

async function doLogin() {
  const token = document.getElementById('login-token').value.trim();
  if (!token) {
    document.getElementById('login-error').textContent = 'Enter a token.';
    return;
  }
  AUTH.token = token;
  localStorage.setItem(TOKEN_KEY, token);
  const ok = await checkAuth();
  if (ok && AUTH.enforced && !AUTH.scope) {
    // whoami returned but didn't authenticate us — treat as a bad token.
    showLogin('That token was not accepted by the server.');
    return;
  }
  hideLogin();
  await init();
}

function signOut() {
  localStorage.removeItem(TOKEN_KEY);
  AUTH = { token: '', scope: null, label: null, enforced: true };
  applyScope();
  updateAuthBar();
  showLogin('Signed out.');
}

function updateAuthBar() {
  const bar = document.getElementById('auth-bar');
  if (!AUTH.enforced || !AUTH.scope) { bar.style.display = 'none'; return; }
  bar.style.display = 'block';
  bar.innerHTML =
    '\\uD83D\\uDD13 ' + (AUTH.label || '?') + ' (' + AUTH.scope + ') \\u00b7 ' +
    '<a href="#" onclick="signOut();return false;">Sign out</a>';
}

function applyScope() {
  CAN_EDIT = (!AUTH.enforced) || AUTH.scope === 'write';
  ['btn-del-selected', 'btn-del-all'].forEach(function (id) {
    const b = document.getElementById(id);
    if (!b) return;
    b.disabled = !CAN_EDIT;
    b.title = CAN_EDIT ? '' : 'Read-only token — editing needs a write token';
  });
}

// Returns true if we may proceed (authenticated, or auth disabled); false if a
// login is required. The data fetches' own 401 handling is the backstop, so a
// missing/older whoami just falls through to "try, and prompt if refused".
async function checkAuth() {
  let res;
  try {
    res = await fetch('/auth/whoami', { headers: authHeaders() });
  } catch (e) {
    AUTH.enforced = false; applyScope(); return true;
  }
  if (res.status === 401) { AUTH.enforced = true; applyScope(); return false; }
  if (!res.ok) { AUTH.enforced = false; applyScope(); return true; }
  let body;
  try { body = await res.json(); } catch (e) {
    AUTH.enforced = false; applyScope(); return true;
  }
  if (body.authenticated) {
    AUTH.enforced = true; AUTH.scope = body.scope; AUTH.label = body.label;
  } else {
    AUTH.enforced = false; AUTH.scope = null; AUTH.label = null;
  }
  applyScope();
  updateAuthBar();
  return true;
}

async function boot() {
  const ok = await checkAuth();
  if (ok) {
    await init();
  } else {
    showLogin('This dashboard requires a token. Paste your monet token to view.');
  }
}

// ── Device color + marker palette (cycles markers when colors repeat) ─────────
const _DEV_COLORS = [
  '#636EFA','#EF553B','#00CC96','#AB63FA','#FFA15A',
  '#19D3F3','#FF6692','#B6E880','#FF97FF','#FECB52',
];
const _DEV_MARKERS = [
  'circle','square','triangle-up','cross','x',
  'triangle-down','star','hexagram','pentagon','diamond',
];
function deviceStyle(idx) {
  const n      = _DEV_COLORS.length;
  const color  = _DEV_COLORS[idx % n];
  const symbol = _DEV_MARKERS[Math.floor(idx / n) % _DEV_MARKERS.length];
  return { color, symbol };
}

// ── Wavelength → color ────────────────────────────────────────────────────────
function wlColor(nm) {
  nm = parseFloat(nm);
  if (nm <= 415) return '#9B30FF';  // violet  ~405
  if (nm <= 460) return '#2255FF';  // blue    ~445
  if (nm <= 510) return '#00AAFF';  // cyan    ~488
  if (nm <= 548) return '#00CC44';  // green   ~532
  if (nm <= 580) return '#AACC00';  // yellow  ~561
  if (nm <= 620) return '#FF8800';  // orange  ~594
  return '#FF2222';                 // red     ~638/647+
}

// ── Model-type detection ──────────────────────────────────────────────────────
function modelType(params) {
  if ('bkg' in params && 'phi' in params) return 'sinusoidal';
  if ('p0'  in params)                    return 'polynomial';
  if (Object.keys(params).length === 1 && 'amp' in params) return 'point';
  return 'unknown';
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-tab') === name);
  });
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  // Allow the browser to paint before resizing Plotly charts
  setTimeout(() => window.dispatchEvent(new Event('resize')), 50);
}

// ── Sidebar helpers ───────────────────────────────────────────────────────────
function selectAll(id)  { for (const o of document.getElementById(id).options) o.selected = true;  }
function selectNone(id) { for (const o of document.getElementById(id).options) o.selected = false; }
function getSelected(id) {
  return Array.from(document.getElementById(id).selectedOptions).map(o => o.value);
}

function populate(id, values, labelFn, valueFn) {
  const sel = document.getElementById(id);
  sel.innerHTML = '';
  values.forEach(v => {
    const opt = document.createElement('option');
    opt.value = valueFn ? String(valueFn(v)) : String(v);
    opt.textContent = labelFn(v);
    sel.appendChild(opt);
  });
}

// ── Initialise sidebar ────────────────────────────────────────────────────────
async function init() {
  showLoading(true);
  try {
    const res  = await authedFetch('/dashboard/api/filters');
    const data = await res.json();
    populate('sel-devices',     data.devices,      v => v,           v => v);
    populate('sel-wavelengths', data.wavelengths,  v => v + '\\u202fnm', v => v);
    populate('sel-powers',      data.laser_powers, v => v + '\\u202fmW', v => v);
    if (data.date_min) document.getElementById('date-from').value = data.date_min;
    if (data.date_max) document.getElementById('date-to').value   = data.date_max;
    selectAll('sel-devices');
    selectAll('sel-wavelengths');
    selectAll('sel-powers');
    await update();
  } catch (err) {
    console.error('init error', err);
  } finally {
    showLoading(false);
  }
}

// ── Fetch and render ──────────────────────────────────────────────────────────
async function update() {
  showLoading(true);
  try {
    const devs = getSelected('sel-devices');
    const wls  = getSelected('sel-wavelengths').map(Number);
    const pows = getSelected('sel-powers').map(Number);
    const body = {
      devices:      devs.length  ? devs  : null,
      wavelengths:  wls.length   ? wls   : null,
      laser_powers: pows.length  ? pows  : null,
      date_from: document.getElementById('date-from').value || null,
      date_to:   document.getElementById('date-to').value   || null,
    };
    const res  = await authedFetch('/dashboard/api/timeseries', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data    = await res.json();
    const records = data.records || [];

    const uniqDevices = new Set(records.map(r => r.device));
    document.getElementById('stat-devices').textContent = uniqDevices.size;
    document.getElementById('stat-records').textContent = records.length;

    renderParamHistory(records);
    renderPowerRange(records);
    renderLatestTable(records);
    renderDatabaseTable(records);
    await renderTransmission();
  } catch (err) {
    console.error('update error', err);
  } finally {
    showLoading(false);
  }
}

function showLoading(show) {
  document.getElementById('loading-overlay').classList.toggle('show', show);
}

// ── Utility ───────────────────────────────────────────────────────────────────
function groupBy(arr, keyFn) {
  return arr.reduce((acc, item) => {
    const k = keyFn(item);
    if (!acc[k]) acc[k] = [];
    acc[k].push(item);
    return acc;
  }, {});
}

function emptyMsg(text) {
  const p = document.createElement('p');
  p.className = 'text-muted';
  p.textContent = text;
  return p;
}

// ═══════════════════════════════════════════════════════════════════════════════
// TAB 1 — Parameter History
// ═══════════════════════════════════════════════════════════════════════════════
function renderParamHistory(records) {
  const container = document.getElementById('charts-param');
  container.innerHTML = '';
  if (!records.length) { container.appendChild(emptyMsg('No data.')); return; }

  const byDevice = groupBy(records, r => r.device);

  for (const [device, recs] of Object.entries(byDevice)) {
    const card     = document.createElement('div');
    card.className = 'chart-card';
    const h6 = document.createElement('h6');
    h6.className   = 'fw-semibold mb-2';
    h6.textContent = device;
    card.appendChild(h6);

    const sinRecs  = recs.filter(r => modelType(r.parameters) === 'sinusoidal');
    const ptRecs   = recs.filter(r => modelType(r.parameters) === 'point');
    const polyRecs = recs.filter(r => modelType(r.parameters) === 'polynomial');

    // Polynomial-only: direct to table tab
    if (!sinRecs.length && !ptRecs.length && polyRecs.length) {
      card.appendChild(emptyMsg(
        'Polynomial calibrations — see the Latest Calibrations tab for parameters.'
      ));
      container.appendChild(card);
      continue;
    }

    const traces = [];
    const layout = {
      margin: { t: 10, b: 45, l: 55, r: 15 },
      hovermode: 'x unified',
      legend: { orientation: 'h', y: -0.18, font: { size: 11 } },
    };

    if (sinRecs.length) {
      // Stacked 3-subplot layout: bkg (top) / amp (mid) / phi (bot)
      layout.yaxis  = { domain: [0.70, 1.00], title: { text: 'bkg', standoff: 4 } };
      layout.yaxis2 = { domain: [0.36, 0.64], title: { text: 'amp', standoff: 4 } };
      layout.yaxis3 = { domain: [0.00, 0.30], title: { text: 'phi', standoff: 4 } };
      layout.xaxis  = { anchor: 'y3' };
      layout.height = 500;

      const sinByWL = groupBy(sinRecs, r => r.wavelength);
      for (const [wl, wlRecs] of Object.entries(sinByWL)) {
        const color = wlColor(wl);
        const xs    = wlRecs.map(r => r.dt);
        const name  = wl + '\\u202fnm';
        const base  = { x: xs, name, legendgroup: name,
                         mode: 'lines+markers', line: { color }, marker: { color } };
        traces.push({ ...base, y: wlRecs.map(r => r.parameters.bkg), yaxis: 'y',
                                showlegend: true });
        traces.push({ ...base, y: wlRecs.map(r => r.parameters.amp), yaxis: 'y2',
                                showlegend: false });
        traces.push({ ...base, y: wlRecs.map(r => r.parameters.phi), yaxis: 'y3',
                                showlegend: false });
      }
    }

    if (ptRecs.length) {
      if (!sinRecs.length) {
        layout.yaxis  = { title: { text: 'amp' } };
        layout.height = 280;
      }
      const ptByWL = groupBy(ptRecs, r => r.wavelength);
      for (const [wl, wlRecs] of Object.entries(ptByWL)) {
        const color = wlColor(wl);
        traces.push({
          x: wlRecs.map(r => r.dt),
          y: wlRecs.map(r => r.parameters.amp),
          name: wl + '\\u202fnm', legendgroup: wl + '\\u202fnm',
          mode: 'lines+markers', line: { color }, marker: { color },
          yaxis: sinRecs.length ? 'y' : 'y',
          showlegend: true,
        });
      }
    }

    const chartDiv       = document.createElement('div');
    chartDiv.className   = 'plotly-chart';
    card.appendChild(chartDiv);
    container.appendChild(card);

    if (traces.length) {
      Plotly.newPlot(chartDiv, traces, layout, { responsive: true, displayModeBar: false });
    } else {
      chartDiv.appendChild(emptyMsg('No plottable records for this device.'));
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// TAB 2 — Power Range
// ═══════════════════════════════════════════════════════════════════════════════
function renderPowerRange(records) {
  const container = document.getElementById('charts-power');
  container.innerHTML = '';
  if (!records.length) { container.appendChild(emptyMsg('No data.')); return; }

  const byDevice = groupBy(records, r => r.device);

  for (const [device, recs] of Object.entries(byDevice)) {
    const card     = document.createElement('div');
    card.className = 'chart-card';
    const h6 = document.createElement('h6');
    h6.className   = 'fw-semibold mb-2';
    h6.textContent = device;
    card.appendChild(h6);

    const traces = [];
    // Sorted unique powers → index determines marker shape
    const sortedPowers = [...new Set(recs.map(r => r.laser_power))].sort((a, b) => a - b);
    const byKey  = groupBy(recs, r => r.wavelength + '__' + r.laser_power);

    for (const [, kRecs] of Object.entries(byKey)) {
      const wl     = kRecs[0].wavelength;
      const lp     = kRecs[0].laser_power;
      const color  = wlColor(wl);
      const symbol = _DEV_MARKERS[sortedPowers.indexOf(lp) % _DEV_MARKERS.length];
      const xs     = kRecs.map(r => r.dt);
      const mt     = modelType(kRecs[0].parameters);
      const name   = wl + '\\u202fnm / ' + lp + '\\u202fmW';

      if (mt === 'sinusoidal') {
        traces.push({
          x: xs, y: kRecs.map(r => r.parameters.bkg + r.parameters.amp),
          name: name + ' max', legendgroup: name,
          mode: 'lines+markers', line: { color }, marker: { color, symbol, size: 7 },
          showlegend: true,
        });
        traces.push({
          x: xs, y: kRecs.map(r => r.parameters.bkg),
          name: name + ' bkg', legendgroup: name,
          mode: 'lines+markers', line: { color, dash: 'dot' }, marker: { color, symbol, size: 7 },
          showlegend: true,
        });
      } else if (mt === 'point') {
        traces.push({
          x: xs, y: kRecs.map(r => r.parameters.amp),
          name, legendgroup: name,
          mode: 'markers', marker: { color, symbol, size: 9 },
          showlegend: true,
        });
      }
    }

    const chartDiv       = document.createElement('div');
    chartDiv.className   = 'plotly-chart';
    card.appendChild(chartDiv);
    container.appendChild(card);

    if (traces.length) {
      Plotly.newPlot(chartDiv, traces, {
        margin: { t: 10, b: 45, l: 55, r: 15 },
        hovermode: 'x unified',
        legend: { orientation: 'h', y: -0.22, font: { size: 11 } },
        yaxis:  { title: 'Power (mW)' },
        height: 300,
      }, { responsive: true, displayModeBar: false });
    } else {
      chartDiv.appendChild(emptyMsg('No plottable records (polynomial models not shown here).'));
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// TAB 3 — Latest Calibrations Table
// ═══════════════════════════════════════════════════════════════════════════════
// ── Latest Calibrations: parameters vs laser-power charts ─────────────────────
function renderLatestCharts(rows) {
  const container = document.getElementById('charts-latest');
  container.innerHTML = '';
  if (!rows.length) return;

  // One chart per wavelength; each series = one microscope.
  // X-axis: laser power setting (mW).  Y-axis: output power (bkg+amp or amp).

  // Assign a consistent style to each device across all wavelength charts.
  const allDevices = [...new Set(rows.map(r => r.device))].sort();
  const devStyleMap = Object.fromEntries(
    allDevices.map((d, i) => [d, deviceStyle(i)])
  );

  const byWL = groupBy(rows, r => r.wavelength);
  const sortedWLs = Object.keys(byWL).map(Number).sort((a, b) => a - b);

  for (const wl of sortedWLs) {
    const wlRecs = byWL[wl].slice().sort((a, b) => a.laser_power - b.laser_power);

    const card = document.createElement('div');
    card.className = 'chart-card';
    const h6 = document.createElement('h6');
    h6.className = 'fw-semibold mb-2';
    h6.style.color = wlColor(wl);
    h6.textContent = wl + '\\u202fnm';
    card.appendChild(h6);

    const traces = [];
    const byDevice = groupBy(wlRecs, r => r.device);

    Object.entries(byDevice).forEach(([device, recs]) => {
      recs.sort((a, b) => a.laser_power - b.laser_power);
      const { color, symbol } = devStyleMap[device];
      const sinRecs = recs.filter(r => modelType(r.parameters) === 'sinusoidal');
      const ptRecs  = recs.filter(r => modelType(r.parameters) === 'point');

      if (sinRecs.length) {
        const sxs = sinRecs.map(r => r.laser_power);
        traces.push({
          x: sxs, y: sinRecs.map(r => r.parameters.bkg + r.parameters.amp),
          name: device + ' max', legendgroup: device,
          mode: 'lines+markers',
          line: { color }, marker: { color, symbol, size: 7 },
          showlegend: true,
        });
        traces.push({
          x: sxs, y: sinRecs.map(r => r.parameters.bkg),
          name: device + ' bkg', legendgroup: device,
          mode: 'lines+markers',
          line: { color, dash: 'dot' }, marker: { color, symbol, size: 7 },
          showlegend: true,
        });
      }
      if (ptRecs.length) {
        traces.push({
          x: ptRecs.map(r => r.laser_power), y: ptRecs.map(r => r.parameters.amp),
          name: device, legendgroup: device,
          mode: 'markers', marker: { color, symbol, size: 9 }, showlegend: true,
        });
      }
    });

    const chartDiv = document.createElement('div');
    chartDiv.className = 'plotly-chart';
    card.appendChild(chartDiv);
    container.appendChild(card);

    if (traces.length) {
      Plotly.newPlot(chartDiv, traces, {
        margin: { t: 10, b: 50, l: 55, r: 15 },
        hovermode: 'x unified',
        legend: { orientation: 'h', y: -0.22, font: { size: 11 } },
        xaxis: { title: 'Laser power setting (mW)' },
        yaxis: { title: 'Power (mW)' },
        height: 300,
      }, { responsive: true, displayModeBar: false });
    } else {
      chartDiv.appendChild(emptyMsg('No plottable records for this wavelength.'));
    }
  }
}

function renderLatestTable(records) {
  const container = document.getElementById('table-latest');
  container.innerHTML = '';
  if (!records.length) {
    document.getElementById('charts-latest').innerHTML = '';
    container.appendChild(emptyMsg('No data.')); return;
  }

  // Keep only the most recent record per (device, wavelength, laser_power)
  const latest = {};
  for (const r of records) {
    const key = r.device + '__' + r.wavelength + '__' + r.laser_power;
    if (!latest[key] || r.dt > latest[key].dt) latest[key] = r;
  }
  const rows = Object.values(latest).sort((a, b) =>
    a.device.localeCompare(b.device) ||
    a.wavelength  - b.wavelength     ||
    a.laser_power - b.laser_power
  );

  renderLatestCharts(rows);

  const wrap = document.createElement('div');
  wrap.className = 'table-responsive';

  const table = document.createElement('table');
  table.className = 'table table-sm table-hover table-bordered align-middle';
  table.innerHTML = `<thead class="table-light"><tr>
    <th>Microscope</th><th>Wavelength</th><th>Power (mW)</th>
    <th>Date</th><th>Time</th><th>Model</th><th>Parameters</th>
  </tr></thead>`;

  const tbody = document.createElement('tbody');
  for (const r of rows) {
    const color    = wlColor(r.wavelength);
    const mt       = modelType(r.parameters);
    const paramStr = JSON.stringify(r.parameters);
    const shortP   = paramStr.length > 58 ? paramStr.slice(0, 55) + '\\u2026' : paramStr;
    const safePS   = paramStr.replace(/&/g,'&amp;').replace(/"/g,'&quot;');

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${r.device}</td>
      <td><span class="wl-dot" style="background:${color}"></span>${r.wavelength}</td>
      <td>${r.laser_power}</td>
      <td>${r.date}</td>
      <td>${r.time}</td>
      <td><span class="badge bg-secondary">${mt}</span></td>
      <td class="params-cell" title="${safePS}">${shortP}</td>`;
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  container.appendChild(wrap);
}

// ═══════════════════════════════════════════════════════════════════════════════
// TAB 4 — All Records (database table with delete)
// ═══════════════════════════════════════════════════════════════════════════════

// Module-level store so delete functions can access current records
let _dbTableRecords = [];

function renderDatabaseTable(records) {
  _dbTableRecords = records.slice();
  const wrap = document.getElementById('db-table-wrap');
  wrap.innerHTML = '';
  document.getElementById('select-all-cb').checked = false;
  document.getElementById('db-table-status').textContent =
    records.length + ' record(s)';

  if (!records.length) {
    wrap.appendChild(emptyMsg('No records match the current filters.'));
    return;
  }

  const table = document.createElement('table');
  table.id = 'db-main-table';
  table.className = 'table table-sm table-hover table-bordered align-middle';

  table.innerHTML = `<thead class="table-light sticky-top"><tr>
    <th style="width:2rem"><input type="checkbox" id="hdr-cb"
        onchange="toggleSelectAll(this.checked)"></th>
    <th>Microscope</th><th>Wavelength</th><th>Power (mW)</th>
    <th>Date</th><th>Time</th><th>PM type</th><th>Parameters</th>
    <th style="width:4rem"></th>
  </tr></thead>`;

  const tbody = document.createElement('tbody');
  records.forEach((r, i) => {
    const color    = wlColor(r.wavelength);
    const pmType   = r.parameters.powermeter_type || '—';
    const paramStr = JSON.stringify(
      Object.fromEntries(
        Object.entries(r.parameters).filter(([k]) => k !== 'powermeter_type')
      ));
    const shortP   = paramStr.length > 50 ? paramStr.slice(0, 47) + '\\u2026' : paramStr;
    const safePS   = paramStr.replace(/&/g,'&amp;').replace(/"/g,'&quot;');

    const tr = document.createElement('tr');
    tr.dataset.idx = i;
    tr.innerHTML = `
      <td><input type="checkbox" class="row-cb" onchange="onRowCbChange()"></td>
      <td>${r.device}</td>
      <td><span class="wl-dot" style="background:${color}"></span>${r.wavelength}</td>
      <td>${r.laser_power}</td>
      <td>${r.date}</td>
      <td>${r.time}</td>
      <td><span class="badge bg-secondary">${pmType}</span></td>
      <td class="params-cell" title="${safePS}">${shortP}</td>
      <td><button class="btn btn-outline-danger btn-sm py-0 px-1"
            onclick="deleteSingle(${i})">✕</button></td>`;
    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  wrap.appendChild(table);
}

function toggleSelectAll(checked) {
  document.querySelectorAll('#db-main-table .row-cb').forEach(cb => {
    cb.checked = checked;
    cb.closest('tr').classList.toggle('selected', checked);
  });
  const hdrCb = document.getElementById('hdr-cb');
  if (hdrCb) hdrCb.checked = checked;
  document.getElementById('select-all-cb').checked = checked;
}

function onRowCbChange() {
  const all  = document.querySelectorAll('#db-main-table .row-cb');
  const chkd = document.querySelectorAll('#db-main-table .row-cb:checked');
  all.forEach(cb => cb.closest('tr').classList.toggle('selected', cb.checked));
  const allChecked = chkd.length === all.length && all.length > 0;
  const hdrCb = document.getElementById('hdr-cb');
  if (hdrCb) hdrCb.checked = allChecked;
  document.getElementById('select-all-cb').checked = allChecked;
}

function _recordToDeletePayload(r) {
  return {
    device_name:       r.device,
    wavelength_nm:     r.wavelength,
    laser_power_mw:    r.laser_power,
    calibration_date:  r.date,
    calibration_time:  r.time,
  };
}

async function _deleteRecords(recsToDelete) {
  let deleted = 0;
  for (const r of recsToDelete) {
    try {
      const res = await authedFetch('/calibrations/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(_recordToDeletePayload(r)),
      });
      if (res.ok) {
        deleted += (await res.json()).deleted_count || 1;
      } else if (res.status === 403) {
        alert('Your token is read-only — deleting needs a write token.');
        break;
      }
    } catch (e) {
      console.error('delete error', e);
    }
  }
  return deleted;
}

function _requireEdit() {
  if (!CAN_EDIT) {
    alert('Read-only token — deleting needs a write token.');
    return false;
  }
  return true;
}

async function deleteSingle(idx) {
  if (!_requireEdit()) return;
  const r = _dbTableRecords[idx];
  if (!confirm(
    `Delete this record?\\n${r.device} | ${r.wavelength} nm | ${r.laser_power} mW | ${r.date} ${r.time}`
  )) return;
  showLoading(true);
  try {
    await _deleteRecords([r]);
    await update();
  } finally { showLoading(false); }
}

async function deleteSelected() {
  if (!_requireEdit()) return;
  const checked = document.querySelectorAll('#db-main-table .row-cb:checked');
  if (!checked.length) { alert('No rows selected.'); return; }
  const idxs  = Array.from(checked).map(cb => parseInt(cb.closest('tr').dataset.idx));
  const recs  = idxs.map(i => _dbTableRecords[i]);
  if (!confirm(`Delete ${recs.length} selected record(s)? This cannot be undone.`)) return;
  showLoading(true);
  try {
    await _deleteRecords(recs);
    await update();
  } finally { showLoading(false); }
}

async function deleteAllInView() {
  if (!_requireEdit()) return;
  const n = _dbTableRecords.length;
  if (!n) { alert('No records in the current view.'); return; }
  if (!confirm(`Delete all ${n} record(s) in the current filtered view? This cannot be undone.`))
    return;
  showLoading(true);
  try {
    await _deleteRecords(_dbTableRecords);
    await update();
  } finally { showLoading(false); }
}

// ═══════════════════════════════════════════════════════════════════════════════
// TAB 5 — Objective Transmission
// ═══════════════════════════════════════════════════════════════════════════════
async function renderTransmission() {
  const container = document.getElementById('charts-transmission');
  container.innerHTML = '';

  let allRecs;
  try {
    const res = await authedFetch('/dashboard/api/transmission_objectives');
    allRecs = await res.json();
  } catch (err) {
    container.appendChild(emptyMsg('Could not load transmission data: ' + err));
    return;
  }

  if (!allRecs.length) {
    container.appendChild(emptyMsg(
      'No transmission_objective data yet. Run both a back focal plane (BFP) ' +
      'and a sample-plane calibration on the same day to compute it.'));
    return;
  }

  // Filter by currently selected devices
  const selDevices = new Set(getSelected('sel-devices'));
  const recs = selDevices.size ? allRecs.filter(r => selDevices.has(r.device)) : allRecs;

  if (!recs.length) {
    container.appendChild(emptyMsg('No data for the selected microscopes.'));
    return;
  }

  const byDevice = groupBy(recs, r => r.device);

  for (const [device, devRecs] of Object.entries(byDevice)) {
    const card     = document.createElement('div');
    card.className = 'chart-card';
    const h6 = document.createElement('h6');
    h6.className   = 'fw-semibold mb-2';
    h6.textContent = device;
    card.appendChild(h6);

    const byWL = groupBy(devRecs, r => r.wavelength);
    const traces = [];

    for (const [wl, wlRecs] of Object.entries(byWL)) {
      wlRecs.sort((a, b) => a.date.localeCompare(b.date));
      const color = wlColor(wl);
      const xs    = wlRecs.map(r => r.date);
      const ys    = wlRecs.map(r => r.transmission_objective_mean);
      const errs  = wlRecs.map(r => r.transmission_objective_std);
      const name  = wl + '\\u202fnm';

      traces.push({
        x: xs,
        y: ys,
        error_y: { type: 'data', array: errs, visible: true, color, thickness: 1.5 },
        name,
        legendgroup: name,
        mode: 'lines+markers',
        line: { color },
        marker: { color, size: 7 },
        customdata: wlRecs.map(r => r.n_points),
        hovertemplate:
          '<b>' + name + '</b><br>Date: %{x}<br>' +
          'T_obj: %{y:.4f} ± %{error_y.array:.4f}<br>' +
          'n = %{customdata}<extra></extra>',
      });
    }

    const chartDiv     = document.createElement('div');
    chartDiv.className = 'plotly-chart';
    card.appendChild(chartDiv);
    container.appendChild(card);

    Plotly.newPlot(chartDiv, traces, {
      margin: { t: 10, b: 45, l: 65, r: 15 },
      hovermode: 'x unified',
      legend: { orientation: 'h', y: -0.22, font: { size: 11 } },
      yaxis: { title: 'transmission_objective (P_sample / P_bfp)' },
      height: 280,
    }, { responsive: true, displayModeBar: false });
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', boot);
</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
def get_dashboard():
    """Serve the interactive dashboard HTML page."""
    return HTMLResponse(content=_DASHBOARD_HTML)
