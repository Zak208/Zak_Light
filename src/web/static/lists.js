'use strict';
/* Zak_light window - static lists, selection marks, mode handling. Classic script: shares globals with the other files (load order in index.html). */
// ---------------------------------------------------------------- building the static parts
function buildLists() {
  const pal = $('#palettes');
  Object.entries(PALETTES).forEach(([key, [name, a, b, c]]) => {
    const el = document.createElement('button');
    el.className = 'swatch'; el.dataset.key = key; el.setAttribute('aria-pressed', 'false');
    el.innerHTML = `<div class="bar3" style="background:linear-gradient(90deg,rgb(${a}),rgb(${b}),rgb(${c}))"></div><span></span>`;
    $('span', el).textContent = name;
    el.onclick = () => { S.palette = key; markPalette(); post('/api/palette', { palette: key }); };
    pal.appendChild(el);
  });

  const pat = $('#patterns');
  PATTERNS.forEach((name, i) => {
    const el = document.createElement('button');
    el.className = 'chip'; el.dataset.i = i; el.textContent = name; el.setAttribute('aria-pressed', 'false');
    el.onclick = () => { markPattern(i); post('/api/settings', { rhythm_pattern: i }); };
    pat.appendChild(el);
  });

  $$('#fx-groups .chip').forEach((c) => { c.onclick = () => { S.fxGroup = c.dataset.group; store.set('zak.fxgroup', S.fxGroup); renderEffects(); }; });

  const wires = $('#wires');
  WIRES.forEach((w) => {
    const el = document.createElement('button');
    el.className = 'chip'; el.dataset.w = w; el.textContent = w; el.setAttribute('aria-pressed', 'false');
    el.onclick = () => { markWire(w); post('/api/settings', { wire_map: w }); };
    wires.appendChild(el);
  });

  const quick = $('#preset-colors');
  QUICK_COLORS.forEach((hex) => {
    const el = document.createElement('button');
    el.style.background = hex; el.setAttribute('aria-label', 'Color ' + hex);
    el.onclick = () => setPickerRgb(hexToRgb(hex), true);
    quick.appendChild(el);
  });

  $$('.stepper').forEach((box) => {
    box.innerHTML = '<button type="button" aria-label="Menos">−</button><input type="number" min="0" max="254" value="0"><button type="button" aria-label="Más">+</button>';
    const input = $('input', box);
    const bump = (d) => { input.value = clamp((+input.value || 0) + d, 0, 254); markZonesDirty(); };
    $$('button', box)[0].onclick = () => bump(-1);
    $$('button', box)[1].onclick = () => bump(1);
    input.oninput = markZonesDirty;
  });
}

const hexToRgb = (hex) => { const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim()); return m ? [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)) : null; };

// ---------------------------------------------------------------- marking the selected things
const setPressed = (els, test) => els.forEach((el) => el.setAttribute('aria-pressed', String(test(el))));
const markPalette = () => setPressed($$('#palettes .swatch'), (el) => el.dataset.key === S.palette);
const markPattern = (i) => setPressed($$('#patterns .chip'), (el) => +el.dataset.i === i);
const markEffect = () => setPressed($$('#effects .swatch'), (el) => el.dataset.key === S.effect);

async function loadEffects() {
  const d = await get('/api/effects');
  if (!d) return;
  S.effects = d.effects;
  const cur = S.effects.find((e) => e.key === S.effect);
  if (cur && !store.get('zak.fxgroup', '')) S.fxGroup = cur.group;
  renderEffects();
}
function renderEffects() {
  setPressed($$('#fx-groups .chip'), (el) => el.dataset.group === S.fxGroup);
  $('#fx-hint').textContent = S.fxGroup === 'ambient' ? 'Suaves y lentos: para dejar puestos toda la tarde.' : 'Rápidos y llamativos: para fiestas y música.';
  const box = $('#effects');
  box.textContent = '';
  S.effects.filter((e) => e.group === S.fxGroup).forEach((e) => {
    const el = document.createElement('button');
    el.className = 'swatch'; el.dataset.key = e.key; el.title = e.description || '';
    el.innerHTML = `<div class="bar3" style="background:${e.swatch}"></div><span></span>`;
    $('span', el).textContent = e.label;
    el.onclick = () => { S.effect = e.key; markEffect(); fxColorsCard(); fillFxColors(true); S.modeTouched = Date.now(); S.mode = 'effect'; S.viewMode = 'effect'; renderMode(); post('/api/effect', { effect: e.key, speed: S.speed }); };
    box.appendChild(el);
  });
  markEffect();
  fxColorsCard();
  fillFxColors(false);
}
function fxColorsCard() {
  const cur = S.effects.find((e) => e.key === S.effect);
  $('#card-fxcolors').hidden = !!cur && !cur.uses_colors;
}

const FX_SWATCHES = ['#ff0000', '#ff6a00', '#ffd000', '#00e030', '#00e5ff', '#0040ff', '#8a2cff', '#ff2d8a', '#ffffff'];

// Shows the colours of the effect in use, named after what each one does in that effect (e.g. "Haz", "Estela")
function fillFxColors(force) {
  const cur = S.effects.find((e) => e.key === S.effect);
  if (!cur) return;
  cur.colors.forEach((rgb, i) => {
    const input = $('#fx-c' + i), slot = $('#slot' + i), role = cur.roles[i];
    slot.hidden = !role;
    $('span', slot).textContent = role || '';
    if (force || document.activeElement !== input) input.value = toHex(rgb);
  });
  $$('.slot').forEach((el, i) => el.classList.toggle('active', i === S.fxSlot));
}
const markWire = (w) => setPressed($$('#wires .chip'), (el) => el.dataset.w === w);
const markPreset = (p) => setPressed($$('#presets .chip'), (el) => el.dataset.preset === p);

// ---------------------------------------------------------------- mode handling
function renderMode() {
  $$('.tile').forEach((t) => t.setAttribute('aria-pressed', String(t.dataset.mode === S.viewMode)));
  $$('.panel').forEach((p) => { p.hidden = p.id !== 'panel-' + S.viewMode; });
  const off = S.mode === 'off';
  $('#modes').classList.toggle('dim', off);
  $('#power').classList.toggle('on', !off);
  $('#preview-off').hidden = !off;
  $('#preview-tag').textContent = off ? 'Apagado' : S.mode === 'calibrate' ? 'Prueba de cableado' : MODES[S.mode] || 'Pantalla';
  $('#test-active').hidden = S.mode !== 'calibrate';
}

function setMode(mode) {
  S.modeTouched = Date.now();
  S.mode = mode;
  if (MODES[mode]) { S.viewMode = mode; S.lastOn = mode; store.set('zak.view', mode); }
  renderMode();
  post('/api/mode', { mode });
  if (mode === 'rhythm') pollSpectrum();
}

function togglePower() { setMode(S.mode === 'off' ? S.lastOn : 'off'); }
