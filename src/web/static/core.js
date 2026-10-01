'use strict';
/* Zak_light window - helpers, constants, shared state, colour maths, sliders.
   Talks to the local engine (same origin); holds no settings of its own: what is on screen is what the engine
   reports (status poll) or what the user just asked for. The other files are classic scripts sharing these
   globals; load order is in index.html. */

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode etc.: not essential */ } },
};

function throttle(fn, ms) {           // runs at most every ms, and always with the last value
  let timer = null, pending = null, last = 0;
  return (...args) => {
    pending = args;
    const wait = ms - (Date.now() - last);
    if (wait <= 0) { last = Date.now(); fn(...args); pending = null; return; }
    if (!timer) timer = setTimeout(() => { timer = null; last = Date.now(); if (pending) fn(...pending); pending = null; }, wait);
  };
}

function toast(text, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = text;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

const QUIET = new Set(['/api/status', '/api/leds', '/api/spectrum']);
async function request(path, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  try {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let msg = 'Error ' + res.status;
      try { const j = await res.json(); msg = (j.details && j.details[0] && `${j.details[0].field}: ${j.details[0].message}`) || j.error || msg; } catch { /* no body */ }
      if (!QUIET.has(path)) toast('No se pudo aplicar: ' + msg, true);
      return null;
    }
    return await res.json();
  } catch {
    if (!QUIET.has(path)) toast('Sin conexión con el motor de Zak_light', true);
    return null;
  }
}
const get = (p) => request(p);
const post = (p, b = {}) => request(p, b);

// ---------------------------------------------------------------- constants
const MODES = { screen: 'Pantalla', rhythm: 'Ritmo', effect: 'Efectos', static: 'Color' };
const PALETTES = {
  cyberpunk: ['Cyberpunk', [0, 220, 255], [255, 0, 140], [160, 20, 255]],
  fire: ['Fuego', [255, 40, 0], [255, 120, 0], [255, 220, 20]],
  neon_blue: ['Neón azul', [0, 60, 240], [0, 190, 255], [220, 245, 255]],
  matrix: ['Matrix', [0, 60, 15], [0, 255, 60], [180, 255, 200]],
  vaporwave: ['Vaporwave', [255, 60, 160], [160, 60, 255], [0, 240, 220]],
  sunset: ['Atardecer', [255, 70, 10], [240, 0, 100], [130, 0, 200]],
};
const PATTERNS = ['Expansión central', 'Flujo lateral', 'Latido', 'Fuego danzante', 'Espectro', 'Onda líquida', 'Resplandor',
  'VU estéreo', 'Espectro perimetral', 'Color por golpe'];
const DAYS = ['L', 'M', 'X', 'J', 'V', 'S', 'D'];
const SCHED_LABEL = { on: 'Encender', off: 'Apagar', mode: 'Modo', profile: 'Perfil', brightness: 'Brillo', sunrise: 'Amanecer' };
const EFFECTS = {
  rainbow: ['Arcoíris', 'linear-gradient(90deg,#f33,#fc3,#3f6,#3cf,#93f,#f3c)'],
  breathing: ['Respiración', 'radial-gradient(circle at 50% 50%,#3ab0ff,#0b2a4a)'],
  fire: ['Fuego', 'linear-gradient(90deg,#ff2d00,#ff8a00,#ffd21a,#ff5a00)'],
  cycle: ['Ciclo de color', 'linear-gradient(90deg,#ff5a8a,#c058ff,#5a8aff,#4cffc6)'],
};
const QUICK_COLORS = ['#ffb464', '#e0f2fe', '#ef4444', '#f97316', '#eab308', '#22c55e', '#06b6d4', '#3b82f6', '#a855f7', '#ec4899'];
const WIRES = ['RGB', 'RBG', 'GRB', 'GBR', 'BRG', 'BGR'];
const BRAND = [168, 85, 247];

// ---------------------------------------------------------------- state
const S = {
  status: null, cfg: {}, mode: 'screen', viewMode: store.get('zak.view', 'screen'), lastOn: store.get('zak.view', 'screen'),
  online: true, view: 'main',
  points: [], colors: [], shown: [], ledRevision: -1, previewDirty: true, previewAnimating: false, zonesSig: '', zonesDirty: false,
  effect: 'rainbow', palette: 'cyberpunk', speed: 1,
  hsv: { h: 270, s: 0.6, v: 1 }, pickerTouched: 0, modeTouched: 0,
  accent: [...BRAND], monitors: [], profiles: [],
  fxSlot: 0, fxTouched: 0,
  effects: [], fxGroup: store.get('zak.fxgroup', 'ambient'), rules: [], schedules: [], schDays: [0, 1, 2, 3, 4, 5, 6], apps: [],
};
if (!MODES[S.viewMode]) S.viewMode = 'screen';

// ---------------------------------------------------------------- color maths
function hsv2rgb(h, s, v) {
  const f = (n) => { const k = (n + h / 60) % 6; return v - v * s * Math.max(0, Math.min(k, 4 - k, 1)); };
  return [Math.round(f(5) * 255), Math.round(f(3) * 255), Math.round(f(1) * 255)];
}
function rgb2hsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d) h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return { h: (h * 60 + 360) % 360, s: mx ? d / mx : 0, v: mx };
}
const toHex = ([r, g, b]) => '#' + [r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('');
function kelvinToRgb(k) {            // Tanner Helland's approximation of black-body colour
  const t = k / 100, c = (v) => Math.round(clamp(v, 0, 255));
  const r = t <= 66 ? 255 : 329.698727446 * Math.pow(t - 60, -0.1332047592);
  const g = t <= 66 ? 99.4708025861 * Math.log(t) - 161.1195681661 : 288.1221695283 * Math.pow(t - 60, -0.0755148492);
  const b = t >= 66 ? 255 : t <= 19 ? 0 : 138.5177312231 * Math.log(t - 10) - 305.0447927307;
  return [c(r), c(g), c(b)];
}

// ---------------------------------------------------------------- sliders
const sliders = {};
function slider(id, { fmt = (v) => v, out, onInput }) {
  const el = $('#' + id), label = out ? $('#' + out) : null;
  const paint = () => {
    el.style.setProperty('--p', ((el.value - el.min) / (el.max - el.min)) * 100 + '%');
    if (label) label.textContent = fmt(+el.value);
  };
  const send = onInput ? throttle((v) => onInput(v), 110) : null;
  el.addEventListener('input', () => { paint(); if (send) send(+el.value); });
  sliders[id] = {
    el,
    set(v) { if (document.activeElement !== el && v !== undefined) { el.value = v; paint(); } },
  };
  paint();
  return sliders[id];
}
