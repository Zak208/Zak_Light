'use strict';
/* Zak_light window - control wiring, keyboard, start-up. Classic script: shares globals with the other files (load order in index.html). */
// ---------------------------------------------------------------- wiring of controls
function bindControls() {
  $$('.tile').forEach((t) => { t.onclick = () => setMode(t.dataset.mode); });
  $('#power').onclick = togglePower;
  $('#open-settings').onclick = () => showView('settings');
  $('#back').onclick = () => showView('main');
  $('#go-zones').onclick = () => { showView('settings'); $('#card-zones').scrollIntoView({ block: 'start' }); };

  slider('sl-brightness', { out: 'v-brightness', fmt: (v) => v + '%', onInput: (v) => post('/api/settings', { brightness: v / 100 }) });
  slider('sl-sens', { out: 'v-sens', fmt: (v) => v.toFixed(1) + '×', onInput: (v) => post('/api/settings', { audio_sensitivity: v }) });
  slider('sl-speed', { out: 'v-speed', fmt: (v) => v.toFixed(1) + '×', onInput: (v) => { S.speed = v; S.modeTouched = Date.now(); post('/api/effect', { effect: S.effect, speed: v }); } });
  slider('sl-gamma', { out: 'v-gamma', fmt: (v) => v.toFixed(1), onInput: (v) => post('/api/settings', { gamma: v }) });
  slider('sl-sat', { out: 'v-sat', fmt: (v) => v.toFixed(2), onInput: (v) => post('/api/settings', { saturation: v }) });
  slider('sl-hdr', { out: 'v-hdr', fmt: (v) => Math.round(v * 100) + '%', onInput: (v) => post('/api/settings', { hdr_compensation: v }) });
  slider('sl-smooth', { out: 'v-smooth', fmt: (v) => v.toFixed(2), onInput: (v) => post('/api/settings', { smoothing: v }) });
  slider('sl-floor', { out: 'v-floor', fmt: (v) => String(v), onInput: (v) => post('/api/settings', { min_luminance_floor: v }) });
  slider('sl-fps', { out: 'v-fps', fmt: (v) => String(v), onInput: (v) => {
    S.cfg.performance_mode = 'custom';
    setPressed($$('#performance-modes .chip'), () => false);
    post('/api/settings', { target_fps: v });
  } });
  $$('#performance-modes .chip').forEach((chip) => {
    chip.onclick = async () => {
      setPressed($$('#performance-modes .chip'), (el) => el === chip);
      if (await setCfg({ performance_mode: chip.dataset.performance })) refresh();
    };
  });
  slider('sl-margin', { out: 'v-margin', fmt: (v) => v.toFixed(1) + '%', onInput: markZonesDirty });
  slider('sl-box', { out: 'v-box', fmt: (v) => v + '%', onInput: markZonesDirty });

  $$('#presets .chip').forEach((c) => { c.onclick = () => { markPreset(c.dataset.preset); post('/api/preset', { preset: c.dataset.preset }).then(refresh); }; });
  $('#sw-bars').onchange = (e) => post('/api/settings', { black_bar_detection: e.target.checked });

  $('#zones-apply').onclick = applyZones;
  $('#zones-reverse').onclick = async () => { const r = await post('/api/auto_layout', { mode: 'reverse' }); if (r) { S.points = r.led_points; toast('Sentido invertido'); } };
  $('#zones-editor').onclick = openEditor;
  $('#editor-done').onclick = closeEditor;
  $('#editor-reset').onclick = async () => { await applyZones(); if (S.editing) buildEditor(); };

  $$('[data-test]').forEach((b) => { b.onclick = () => { S.modeTouched = Date.now(); S.mode = 'calibrate'; renderMode(); post('/api/calibrate_action', { action: b.dataset.test }); }; });
  $('#test-stop').onclick = async () => { await post('/api/calibrate_action', { action: 'stop' }); S.modeTouched = 0; refresh(); };

  $('#profile-save').onclick = async () => {
    const name = $('#profile-name').value.trim();
    if (!name) return toast('Escribe un nombre para el perfil', true);
    if (await post('/api/profiles', { name })) { toast(`Perfil “${name}” guardado`); $('#profile-name').value = ''; loadProfiles(); }
  };
  $('#profile-load').onclick = () => applyProfile($('#profile-list').value);
  $('#profile-delete').onclick = async () => {
    const name = $('#profile-list').value;
    if (name && confirm(`¿Borrar el perfil “${name}”?`) && await post('/api/profiles/delete', { name })) { toast('Perfil borrado'); loadProfiles(); }
  };
  $('#quick-profile').onchange = (e) => { applyProfile(e.target.value); };

  $('#sw-autostart').onchange = (e) => post('/api/startup', { autostart: e.target.checked });
  $('#sw-lowpower').onchange = (e) => post('/api/startup', { low_power_ui: e.target.checked });
  $('#audio-device').onchange = async (e) => {
    if (await setCfg({ audio_device_id: e.target.value })) {
      toast('Salida de audio actualizada');
      loadAudioDevices();
    }
  };
  $('#audio-refresh').onclick = loadAudioDevices;
  $('#open-data').onclick = () => post('/api/open_data_dir');
  $('#reset').onclick = async () => { if (confirm('¿Restablecer imagen, audio y calidad de luz a sus valores de fábrica?\nTambién se olvidará la calibración guardada de este monitor. Tus cables y zonas no cambian.') && await post('/api/reset_defaults')) { toast('Imagen restablecida'); refresh(); } };
  $('#quit').onclick = () => { if (confirm('¿Cerrar Zak_light por completo? La tira se apagará.')) post('/api/quit'); };

  // screen behaviour
  $$('#styles .style-card').forEach((c) => { c.onclick = () => { setPressed($$('#styles .style-card'), (el) => el === c); setCfg({ screen_style: c.dataset.style }); }; });
  $$('#edges .chip').forEach((c) => {
    c.onclick = () => {
      c.setAttribute('aria-pressed', String(c.getAttribute('aria-pressed') !== 'true'));
      const on = $$('#edges .chip').map((el) => el.getAttribute('aria-pressed') === 'true');
      if (!on.some(Boolean)) { c.setAttribute('aria-pressed', 'true'); return toast('Al menos un lado debe seguir activo', true); }
      setCfg({ edges_enabled: on });
    };
  });
  slider('sl-boost', { out: 'v-boost', fmt: (v) => Math.round(v * 100) + '%', onInput: (v) => setCfg({ audio_boost: v }) });

  // effect colours
  // Colours are stored per effect: they are sent only when one was changed, so switching direction never "locks" a palette
  const sendFx = throttle((withColors) => {
    const body = { effect: S.effect, speed: S.speed, reverse: $('#sw-reverse').checked, mirror: $('#sw-mirror').checked };
    if (withColors === true) {
      body.colors = [0, 1, 2].map((i) => hexToRgb($('#fx-c' + i).value) || [255, 255, 255]);
      const cur = S.effects.find((e) => e.key === S.effect);
      if (cur) cur.colors = body.colors;
      S.fxTouched = Date.now();
    }
    S.modeTouched = Date.now(); S.mode = 'effect'; S.viewMode = 'effect'; renderMode();
    post('/api/effect', body);
  }, 150);
  [0, 1, 2].forEach((i) => {
    const input = $('#fx-c' + i);
    input.addEventListener('input', () => sendFx(true));
    const pick = () => { S.fxSlot = i; $$('.slot').forEach((el, k) => el.classList.toggle('active', k === i)); };
    input.addEventListener('focus', pick); input.addEventListener('click', pick);
  });
  FX_SWATCHES.forEach((hex) => {
    const b = document.createElement('button');
    b.style.background = hex; b.setAttribute('aria-label', 'Color puro ' + hex); b.title = hex;
    b.onclick = () => { $('#fx-c' + S.fxSlot).value = hex; sendFx(true); };
    $('#fx-swatches').appendChild(b);
  });
  $('#fx-reset').onclick = async () => {
    if (await post('/api/effect', { effect: S.effect, speed: S.speed, reset_colors: true })) {
      await loadEffects(); fillFxColors(true); S.fxTouched = 0; toast('Colores restablecidos');
    }
  };
  $('#sw-reverse').onchange = () => sendFx(false); $('#sw-mirror').onchange = () => sendFx(false);

  // light quality
  const wbSliders = () => [sliders['sl-wb-r'], sliders['sl-wb-g'], sliders['sl-wb-b']];
  const wb = () => setCfg({ white_balance: wbSliders().map((s) => +s.el.value) });
  slider('sl-wb-r', { out: 'v-wb-r', fmt: (v) => v.toFixed(2), onInput: wb });
  slider('sl-wb-g', { out: 'v-wb-g', fmt: (v) => v.toFixed(2), onInput: wb });
  slider('sl-wb-b', { out: 'v-wb-b', fmt: (v) => v.toFixed(2), onInput: wb });
  slider('sl-dark', { out: 'v-dark', fmt: (v) => Math.round(v * 100) + '%', onInput: (v) => setCfg({ dark_light: v }) });
  slider('sl-power', { out: 'v-power', fmt: (v) => Math.round(v * 100) + '%', onInput: (v) => setCfg({ max_power: v }) });
  $('#sw-dither').onchange = (e) => setCfg({ dithering: e.target.checked });
  $('#sw-adapt').onchange = (e) => setCfg({ adaptive_smoothing: e.target.checked });
  $('#sw-trans').onchange = (e) => setCfg({ transitions: e.target.checked });
  $('#monitor-color-save').onclick = async () => {
    if (await post('/api/monitor_color_profile', { action: 'save' })) { toast('Calibración guardada para este monitor'); refresh(); }
  };
  $('#monitor-color-load').onclick = async () => {
    if (await post('/api/monitor_color_profile', { action: 'load' })) { toast('Calibración restaurada'); refresh(); }
  };
  $('#monitor-color-delete').onclick = async () => {
    if (confirm('¿Olvidar la calibración guardada de este monitor?') && await post('/api/monitor_color_profile', { action: 'delete' })) {
      toast('Calibración olvidada'); refresh();
    }
  };

  bindAutomation();
  $('#wiz-go').onclick = () => { WIZ.colors = []; WIZ.step = 0; wizChannel(); };
  $('#export').onclick = exportSettings;
  $('#import').onclick = () => $('#import-file').click();
  $('#import-file').onchange = (e) => { importSettings(e.target.files[0]); e.target.value = ''; };
  $('#diagnostics-export').onclick = exportDiagnostics;
  $('#measurement-start').onclick = async () => {
    const scenario = $('#measurement-scenario').value;
    const r = await post('/api/performance_measurement', { action: 'start', scenario });
    if (r) { renderMeasurement(r.measurement); toast('Medición iniciada'); }
  };
  $('#measurement-stop').onclick = async () => {
    const r = await post('/api/performance_measurement', { action: 'stop' });
    if (r) { renderMeasurement(r.measurement); toast(r.stopped ? 'Medición terminada' : 'No había una medición activa'); }
  };
  $('#measurement-export').onclick = exportPerformanceMeasurement;

  window.addEventListener('resize', sizeZones);
}

async function loadMonitors() {
  const d = await get('/api/monitors');
  if (!d) return;
  S.monitors = d.monitors;
  const box = $('#monitor-list');
  box.textContent = '';
  d.monitors.forEach((m) => {
    const b = document.createElement('button');
    b.className = 'chip'; b.dataset.i = m.index; b.textContent = m.label; b.setAttribute('aria-pressed', 'false');
    b.onclick = async () => {
      setPressed($$('#monitor-list .chip'), (el) => el === b);
      if (await post('/api/settings', { monitor_index: m.index })) refresh();
    };
    box.appendChild(b);
  });
  $('#monitor-row').hidden = d.monitors.length < 2;
}

// ---------------------------------------------------------------- keyboard
document.addEventListener('keydown', (e) => {
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (e.key === 'Escape') { if (S.editing) closeEditor(); else if (S.view === 'settings') showView('main'); return; }
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || e.ctrlKey || e.altKey || e.metaKey) return;
  if (e.key === ' ' && tag !== 'BUTTON') { e.preventDefault(); togglePower(); }
  else if (e.key >= '1' && e.key <= '4') { showView('main'); setMode(['screen', 'rhythm', 'effect', 'static'][+e.key - 1]); }
  else if (e.key === ',') showView('settings');
  else if (['+', '=', '-', '_'].includes(e.key)) {
    const el = sliders['sl-brightness'].el, v = clamp(+el.value + (e.key === '+' || e.key === '=' ? 5 : -5), 5, 100);
    el.value = v; el.dispatchEvent(new Event('input'));
  }
});

// ---------------------------------------------------------------- start
async function poll() {
  if (!document.hidden) await refresh();
  setTimeout(poll, 2000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); startPreview(); } else stopLoops(); });

buildLists();
$$('.row').forEach((r) => { if ($('input[type="range"]', r) && $('.label', r)) r.classList.add('sl'); });
bindControls();
initPicker();
renderMode();
resizePreview();
loadMonitors();
loadProfiles();
loadEffects();
refresh().then(() => { startPreview(); if (S.mode === 'rhythm') pollSpectrum(); });
poll();
