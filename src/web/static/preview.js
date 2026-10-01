'use strict';
/* Zak_light window - live preview, spectrum, colour picker. Classic script: shares globals with the other files (load order in index.html). */
// ---------------------------------------------------------------- live preview
const cv = $('#preview'), ctx = cv.getContext('2d');
let cw = 0, ch = 0, rafId = 0, lastDraw = 0;

function resizePreview() {
  const r = cv.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
  cw = r.width; ch = r.height;
  cv.width = Math.round(cw * dpr); cv.height = Math.round(ch * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  S.previewDirty = true;
  schedulePreview();
}
new ResizeObserver(resizePreview).observe(cv);

function drawPreview() {
  ctx.clearRect(0, 0, cw, ch);
  let mw = cw * 0.64, mh = mw * 9 / 16;
  if (mh > ch * 0.6) { mh = ch * 0.6; mw = mh * 16 / 9; }
  const mx = (cw - mw) / 2, my = ch * 0.14, gap = 7;

  // smooth towards the colors the engine reported
  const n = S.points.length;
  if (S.shown.length !== n) S.shown = Array.from({ length: n }, () => [0, 0, 0]);
  let animating = false;
  for (let i = 0; i < n; i++) {
    const t = S.colors[i] || [0, 0, 0], c = S.shown[i];
    for (let k = 0; k < 3; k++) {
      const delta = t[k] - c[k];
      c[k] += delta * 0.3;
      animating ||= Math.abs(delta) > 0.7;
    }
  }

  // the strip: soft light on the wall behind the monitor
  ctx.globalCompositeOperation = 'lighter';
  const radius = mw * 0.085;
  for (let i = 0; i < n; i++) {
    const [x, y] = S.points[i], c = S.shown[i];
    if (c[0] + c[1] + c[2] < 6) continue;
    const d = [x, 1 - x, y, 1 - y], e = d.indexOf(Math.min(...d));
    const px = e === 0 ? mx - gap : e === 1 ? mx + mw + gap : mx + clamp(x, 0, 1) * mw;
    const py = e === 2 ? my - gap : e === 3 ? my + mh + gap : my + clamp(y, 0, 1) * mh;
    const g = ctx.createRadialGradient(px, py, 0, px, py, radius);
    const col = `${c[0] | 0},${c[1] | 0},${c[2] | 0}`;
    g.addColorStop(0, `rgba(${col},0.95)`); g.addColorStop(0.35, `rgba(${col},0.45)`); g.addColorStop(1, `rgba(${col},0)`);
    ctx.fillStyle = g; ctx.fillRect(px - radius, py - radius, radius * 2, radius * 2);
  }
  ctx.globalCompositeOperation = 'source-over';

  // the monitor
  const r = 10;
  ctx.beginPath(); ctx.roundRect(mx, my, mw, mh, r);
  ctx.fillStyle = '#0c0e17'; ctx.fill();
  ctx.lineWidth = 1.5; ctx.strokeStyle = 'rgba(255,255,255,0.18)'; ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cw / 2 - mw * 0.09, my + mh + 1); ctx.lineTo(cw / 2 + mw * 0.09, my + mh + 1);
  ctx.lineTo(cw / 2 + mw * 0.15, my + mh + 12); ctx.lineTo(cw / 2 - mw * 0.15, my + mh + 12); ctx.closePath();
  ctx.fillStyle = 'rgba(255,255,255,0.08)'; ctx.fill();
  return animating;
}

function previewActive() { return S.view === 'main' && !document.hidden; }

function previewLoop(ts) {
  rafId = 0;
  if (!previewActive()) return;
  if (S.previewDirty || S.previewAnimating) {
    if (ts - lastDraw > 33) {
      lastDraw = ts;
      S.previewDirty = false;
      S.previewAnimating = drawPreview();
    }
    if (S.previewDirty || S.previewAnimating) rafId = requestAnimationFrame(previewLoop);
  }
}
function schedulePreview() {
  if (!rafId && previewActive()) rafId = requestAnimationFrame(previewLoop);
}
function startPreview() {
  S.previewDirty = true;
  schedulePreview();
  pollLeds();
  if (S.mode === 'rhythm') pollSpectrum();
}

let ledsRunning = false;
async function pollLeds() {
  if (ledsRunning) return;
  ledsRunning = true;
  try {
    while (previewActive() && S.online) {
      const d = await get('/api/leds?since=' + S.ledRevision);
      if (d) {
        S.ledRevision = d.revision;
        if (d.known && !d.unchanged) {
          S.colors = d.colors;
          updateAccent(d.colors);
          S.previewDirty = true;
          schedulePreview();
        }
      }
      const quiet = S.mode === 'off' || S.mode === 'static';
      const rate = S.cfg.performance_mode === 'eco' ? 180 : S.cfg.performance_mode === 'fluid' ? 75 : 100;
      await sleep(quiet ? rate * 5 : rate);
    }
  } finally { ledsRunning = false; }
}

function updateAccent(colors) {
  let r = 0, g = 0, b = 0, w = 0;
  for (const c of colors) { const k = Math.max(c[0], c[1], c[2]) / 255; r += c[0] * k; g += c[1] * k; b += c[2] * k; w += k; }
  let target = BRAND;
  if (w > 0.5) {
    let avg = [r / w, g / w, b / w];
    const m = Math.max(...avg), lift = m > 0 ? Math.min(255 / m, 2.2) : 1;
    avg = avg.map((v) => clamp(v * lift, 0, 255));
    const lum = 0.2126 * avg[0] + 0.7152 * avg[1] + 0.0722 * avg[2];
    target = lum < 70 ? avg.map((v, i) => (v + BRAND[i]) / 2) : avg;
  }
  S.accent = S.accent.map((v, i) => v + (target[i] - v) * 0.25);
  document.documentElement.style.setProperty('--accent-rgb', S.accent.map(Math.round).join(','));
}

// ---------------------------------------------------------------- spectrum (Ritmo)
let specRunning = false;
async function pollSpectrum() {
  if (specRunning) return;
  specRunning = true;
  const bars = $$('#eq i');
  try {
    while (previewActive() && S.mode === 'rhythm' && S.viewMode === 'rhythm' && S.online) {
      const d = await get('/api/spectrum');
      if (d) bars.forEach((el, i) => { el.style.height = Math.round(4 + (d.bars[i] || 0) * 50) + 'px'; });
      await sleep(S.cfg.performance_mode === 'eco' ? 120 : S.cfg.performance_mode === 'fluid' ? 55 : 80);
    }
  } finally { specRunning = false; }
}

// ---------------------------------------------------------------- color picker
const sv = $('#sv'), svCanvas = $('#sv-canvas'), hue = $('#hue');

function drawSV() {
  const c = svCanvas.getContext('2d'), w = svCanvas.width, h = svCanvas.height;
  c.fillStyle = `hsl(${S.hsv.h},100%,50%)`; c.fillRect(0, 0, w, h);
  let g = c.createLinearGradient(0, 0, w, 0); g.addColorStop(0, '#fff'); g.addColorStop(1, 'rgba(255,255,255,0)');
  c.fillStyle = g; c.fillRect(0, 0, w, h);
  g = c.createLinearGradient(0, 0, 0, h); g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, '#000');
  c.fillStyle = g; c.fillRect(0, 0, w, h);
}
function paintPicker() {
  drawSV();
  $('#sv-knob').style.left = S.hsv.s * 100 + '%'; $('#sv-knob').style.top = (1 - S.hsv.v) * 100 + '%';
  $('#hue-knob').style.left = (S.hsv.h / 360) * 100 + '%';
  const rgb = hsv2rgb(S.hsv.h, S.hsv.s, S.hsv.v), hex = toHex(rgb);
  $('#sv-knob').style.background = hex; $('#hue-knob').style.background = `hsl(${S.hsv.h},100%,50%)`;
  if (document.activeElement !== $('#hex')) $('#hex').value = hex.toUpperCase();
  return rgb;
}
const sendColor = throttle((rgb) => {
  S.modeTouched = Date.now(); S.mode = 'static'; S.viewMode = 'static'; renderMode();
  post('/api/static_color', { r: rgb[0], g: rgb[1], b: rgb[2] });
}, 120);

function setPickerRgb(rgb, send) {
  S.hsv = rgb2hsv(...rgb);
  const out = paintPicker();
  if (send) { S.pickerTouched = Date.now(); sendColor(out); }
}
function dragOn(el, fn) {
  const move = (e) => { const r = el.getBoundingClientRect(); fn(clamp((e.clientX - r.left) / r.width, 0, 1), clamp((e.clientY - r.top) / r.height, 0, 1)); };
  el.addEventListener('pointerdown', (e) => { el.setPointerCapture(e.pointerId); move(e); el.onpointermove = move; });
  el.addEventListener('pointerup', () => { el.onpointermove = null; });
}
function initPicker() {
  dragOn(sv, (x, y) => { S.hsv.s = x; S.hsv.v = 1 - y; S.pickerTouched = Date.now(); sendColor(paintPicker()); });
  dragOn(hue, (x) => { S.hsv.h = x * 360; S.pickerTouched = Date.now(); sendColor(paintPicker()); });
  const hexInput = $('#hex');
  const commit = () => { const rgb = hexToRgb(hexInput.value); if (rgb) setPickerRgb(rgb, true); else hexInput.value = toHex(hsv2rgb(S.hsv.h, S.hsv.s, S.hsv.v)).toUpperCase(); };
  hexInput.addEventListener('change', commit);
  hexInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { commit(); hexInput.blur(); } });
  $('#sl-kelvin').addEventListener('input', (e) => { const el = e.target; el.style.setProperty('--p', ((el.value - el.min) / (el.max - el.min)) * 100 + '%'); setPickerRgb(kelvinToRgb(+el.value), true); });
  $('#sl-kelvin').style.setProperty('--p', '36%');
  paintPicker();
}
