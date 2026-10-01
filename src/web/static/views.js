'use strict';
/* Zak_light window - views, zones editor, profiles. Classic script: shares globals with the other files (load order in index.html). */
// ---------------------------------------------------------------- views
function showView(name) {
  S.view = name;
  $('#view-main').hidden = name !== 'main';
  $('#view-settings').hidden = name !== 'settings';
  $('#footer .bar').hidden = name === 'settings';
  if (name === 'settings') { loadSettingsData(); stopLoops(); } else { startPreview(); resizePreview(); }
  $(name === 'main' ? '#view-main' : '#view-settings').scrollTop = 0;
}
function stopLoops() { cancelAnimationFrame(rafId); rafId = 0; }  // the polling loops end on their own when the view is hidden

async function loadSettingsData() {
  const s = await get('/api/startup');
  if (s) { $('#sw-autostart').checked = s.autostart; $('#sw-lowpower').checked = s.low_power_ui; sliders['sl-autoclose'].set(s.ui_autoclose_minutes); $('#v-autoclose').textContent = s.ui_autoclose_minutes ? s.ui_autoclose_minutes + ' min' : 'Nunca'; }
  await Promise.all([loadProfiles(), loadAudioDevices()]);
  loadAutomation();
}

async function loadAudioDevices() {
  const d = await get('/api/audio_devices');
  if (!d) return;
  const select = $('#audio-device');
  select.textContent = '';
  const devices = d.devices || [];
  devices.forEach((device) => {
    const option = document.createElement('option');
    option.value = device.id;
    option.textContent = device.default && device.id ? `${device.name} (predeterminada actual)` : device.name;
    select.appendChild(option);
  });
  if (!devices.some((device) => device.id === d.selected)) {
    const missing = document.createElement('option');
    missing.value = d.selected;
    missing.textContent = 'Salida guardada no disponible';
    select.appendChild(missing);
  }
  select.value = d.selected || '';
  const status = d.status || {};
  $('#audio-device-note').textContent = !status.available
    ? 'La captura de audio no está disponible en esta instalación.'
    : status.last_error
      ? 'No se pudo abrir la salida elegida. Conecta el dispositivo o vuelve a la salida predeterminada.'
      : status.using_fallback
        ? 'La salida elegida no está disponible; Ritmo usa temporalmente la salida predeterminada de Windows.'
      : status.active
        ? 'Capturando audio para el modo Ritmo.'
        : 'Solo se activa mientras usas el modo Ritmo.';
}

// ---------------------------------------------------------------- zones
function markZonesDirty() { S.zonesDirty = true; }
const edgeValue = (e) => clamp(parseInt($(`.stepper[data-edge="${e}"] input`).value, 10) || 0, 0, 254);

async function applyZones() {
  const body = {
    leds_left: edgeValue('left'), leds_top: edgeValue('top'), leds_right: edgeValue('right'), leds_bottom: edgeValue('bottom'),
    margin: +sliders['sl-margin'].el.value / 100, box_size: +sliders['sl-box'].el.value / 100,
  };
  const r = await post('/api/zones', body);
  if (r) { S.zonesDirty = false; S.points = r.led_points; S.zonesSig = ''; toast(`Zonas aplicadas (${r.led_count} LED)`); refresh(); }
}

// ---- advanced editor (the dots live only here)
function buildEditor() {
  const screen = $('#editor-screen');
  screen.textContent = '';
  const box = (S.cfg.sample_box_size || 0.06);
  S.points.forEach((p, i) => {
    const el = document.createElement('div');
    el.className = 'led'; el.dataset.i = i + 1;
    el.style.left = p[0] * 100 + '%'; el.style.top = p[1] * 100 + '%';
    const zone = document.createElement('div'); zone.className = 'zone';
    zone.style.width = box * 100 + 'vw'; zone.style.height = box * 100 + 'vh';
    el.appendChild(zone);
    el.addEventListener('pointerdown', (e) => {
      e.preventDefault(); el.setPointerCapture(e.pointerId); el.classList.add('drag');
      const r = screen.getBoundingClientRect();
      el.onpointermove = (ev) => {
        const x = clamp((ev.clientX - r.left) / r.width, 0, 1), y = clamp((ev.clientY - r.top) / r.height, 0, 1);
        S.points[i] = [+x.toFixed(4), +y.toFixed(4)];
        el.style.left = x * 100 + '%'; el.style.top = y * 100 + '%';
      };
    });
    const end = () => { if (!el.classList.contains('drag')) return; el.classList.remove('drag'); el.onpointermove = null; post('/api/led_points', { points: S.points }).then(() => { S.zonesSig = ''; }); };
    el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
    screen.appendChild(el);
  });
  sizeZones();
}
function sizeZones() {
  const r = $('#editor-screen').getBoundingClientRect(), box = S.cfg.sample_box_size || 0.06;
  $$('#editor-screen .zone').forEach((z) => { z.style.width = r.width * box + 'px'; z.style.height = r.height * box + 'px'; });
}
function openEditor() { S.editing = true; $('#editor').hidden = false; buildEditor(); }
function closeEditor() { S.editing = false; $('#editor').hidden = true; refresh(); }

// ---------------------------------------------------------------- profiles
async function loadProfiles() {
  const d = await get('/api/profiles');
  if (!d) return;
  S.profiles = d.profiles;
  const list = $('#profile-list'), quick = $('#quick-profile');
  list.textContent = ''; quick.textContent = '';
  const first = document.createElement('option'); first.value = ''; first.textContent = 'Perfiles…'; quick.appendChild(first);
  d.profiles.forEach((n) => {
    [list, quick].forEach((sel) => { const o = document.createElement('option'); o.value = n; o.textContent = n; sel.appendChild(o); });
  });
  markActiveProfile(d.active);
  if (!d.profiles.length) { const o = document.createElement('option'); o.value = ''; o.textContent = '(sin perfiles guardados)'; list.appendChild(o); }
}
async function applyProfile(name) {
  if (!name) return;
  if (await post('/api/profiles/load', { name })) { toast(`Perfil “${name}” aplicado`); refresh(); }
}
