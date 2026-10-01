'use strict';
/* Zak_light window - engine status -> screen. Classic script: shares globals with the other files (load order in index.html). */
// ---------------------------------------------------------------- status -> screen
function setOnline(online) {
  if (online === S.online) return;
  S.online = online;
  if (online) startPreview();
  if (!online) {
    showBanner('bad', 'Se perdió la conexión con el motor de Zak_light. Si lo cerraste desde la bandeja, vuelve a abrir el programa.');
    $('#status-text').textContent = 'Motor sin respuesta';
    $('#status-dot').className = 'dot bad';
  }
}

function showBanner(kind, text, actions = []) {
  const b = $('#banner');
  if (!text) { b.hidden = true; return; }
  b.className = 'banner' + (kind === 'bad' ? ' bad' : '');
  b.textContent = '';
  const p = document.createElement('div'); p.textContent = text; b.appendChild(p);
  if (actions.length) {
    const row = document.createElement('div'); row.className = 'actions';
    actions.forEach(([label, fn]) => { const btn = document.createElement('button'); btn.className = 'btn small warn'; btn.textContent = label; btn.onclick = fn; row.appendChild(btn); });
    b.appendChild(row);
  }
  b.hidden = false;
}

function applyConnection(d) {
  const dot = $('#status-dot'), text = $('#status-text');
  if (d.suspended) { dot.className = 'dot warn'; text.textContent = 'En pausa (pantalla apagada)'; }
  else if (d.connected) { dot.className = 'dot ok'; text.textContent = `Conectado · ${d.config.led_count} LED`; }
  else { dot.className = 'dot bad'; text.textContent = 'Sin controlador USB'; }

  if (d.conflicts && d.conflicts.length && !d.suspended) {
    const names = d.conflicts.map((c) => c.name).join(', ');
    const actions = [[`Cerrar ${names}`, async () => { if (await post('/api/conflicts/close')) { toast('Programa cerrado'); refresh(); } }]];
    if (d.conflicts.some((c) => c.autostart)) actions.push(['Que no arranque con Windows', async () => { if (await post('/api/conflicts/disable_autostart')) { toast('Ya no arrancará con Windows'); refresh(); } }]);
    showBanner('warn', `${names} está abierto y le envía imágenes al mismo controlador: por eso los colores no cambian o vuelven a la sincronización de pantalla. Solo un programa puede manejar la tira.`, actions);
  } else if (d.capture_stalled && !d.suspended && S.mode === 'screen') {
    showBanner('warn', 'La captura de pantalla no responde (puede pasar al cambiar de resolución o con un juego en pantalla completa exclusiva). Zak_light sigue intentando recuperarla.');
  } else if (d.hdr && !d.suspended && S.mode === 'screen') {
    showBanner('warn', 'Windows tiene HDR activado: los colores de la pantalla pueden verse lavados. Puedes desactivarlo o ajustar una compensación visual manual.', [
      ['Ajustar compensación', () => { showView('settings'); setTimeout(() => $('#sl-hdr').scrollIntoView({ block: 'center' }), 0); }],
    ]);
  } else if (!d.connected && !d.suspended) {
    showBanner('bad', 'No se encuentra el controlador de la tira. Revisa el cable USB. Zak_light lo reintenta solo.');
  } else {
    showBanner('', '');
  }
}

function applyStatus(d) {
  S.status = d; S.cfg = d.config;
  applyConnection(d);

  // Do not let a reply that was already on its way undo what the user has just pressed
  if (Date.now() - S.modeTouched > 2500) {
    S.mode = d.mode;
    if (MODES[d.mode]) { S.viewMode = d.mode; S.lastOn = d.mode; }
    S.effect = d.active_effect || S.effect;
  }
  S.palette = d.active_palette || S.palette;
  S.speed = Date.now() - S.modeTouched > 2500 ? (d.config.effect_speed || 1) : S.speed;
  renderMode();

  const c = d.config;
  sliders['sl-brightness'].set(Math.round(c.brightness * 100));
  $('#v-brightness').textContent = Math.round(sliders['sl-brightness'].el.value) + '%';
  sliders['sl-sens'].set(c.audio_sensitivity);
  sliders['sl-speed'].set(S.speed);
  sliders['sl-gamma'].set(c.gamma);
  sliders['sl-sat'].set(c.saturation);
  sliders['sl-hdr'].set(c.hdr_compensation);
  sliders['sl-smooth'].set(c.smoothing);
  sliders['sl-floor'].set(c.min_luminance_floor);
  sliders['sl-fps'].set(c.target_fps);
  setPressed($$('#performance-modes .chip'), (el) => el.dataset.performance === c.performance_mode);
  if (!S.zonesDirty) {
    sliders['sl-margin'].set(+(c.edge_margin * 100).toFixed(1));
    sliders['sl-box'].set(Math.round(c.sample_box_size * 100));
    ['left', 'top', 'right', 'bottom'].forEach((e) => { const i = $(`.stepper[data-edge="${e}"] input`); if (document.activeElement !== i) i.value = c['leds_' + e]; });
  }
  $('#sw-bars').checked = c.black_bar_detection;
  $('#zones-note').hidden = !c.zones_customized;
  $('#zones-summary').textContent = `${c.led_count} LED · ${c.zones_customized ? 'colocados a mano' : 'reparto automático'}`;
  markPreset(c.sync_preset); markPattern(c.rhythm_pattern); markWire(c.wire_map); markPalette(); markEffect();
  setPressed($$('#monitor-list .chip'), (el) => +el.dataset.i === c.monitor_index);

  syncV2(d);
  if (S.viewMode === 'static' && S.view === 'main' && Date.now() - S.pickerTouched > 2500 && c.static_color) setPickerRgb(c.static_color, false);

  const sig = [c.led_count, c.leds_left, c.leds_top, c.leds_right, c.leds_bottom, c.edge_margin, c.zones_customized].join('|');
  if (sig !== S.zonesSig) { S.zonesSig = sig; loadPoints(); }
  $('#about').textContent = `Zak_light v${d.version} · captura: ${d.capture_backend} · motor ${d.metrics.ram_mb} MB, ${d.metrics.cpu_pct}% CPU`;
  const cap = d.engine.capture || {};
  const capState = d.capture_stalled ? 'atascada' : (cap.inflight ? 'trabajando' : 'en espera');
  const helperInfo = cap.isolated ? ` · proceso aparte${cap.helper_restarts ? `, reiniciado ${cap.helper_restarts}×` : ''}` : '';
  const captureIssue = cap.last_error_type ? ` · último fallo ${cap.last_error_type}${cap.last_error_age_s != null ? ` hace ${Math.round(cap.last_error_age_s)} s` : ''}` : '';
  $('#diagnostics').textContent = `Captura ${d.capture_backend}: ${cap.fps ?? 0} FPS · ${Math.round(d.metrics.frame_ms)} ms/fotograma · latencia ${Math.round(d.engine.latency_ms)} ms · USB ${d.engine.usb_writes} envíos (${d.engine.usb_writes_per_min ?? 0}/min), ${d.engine.usb_failures} fallos · capturador ${capState}${cap.restart_pending ? ' (reinicio seguro pendiente)' : ''}${helperInfo}${captureIssue}.`;
  renderMeasurement(d.performance_measurement);
}

const untouched = (el) => document.activeElement !== el;
function markActiveProfile(name) {
  S.activeProfile = name || '';
  const quick = $('#quick-profile'), list = $('#profile-list');
  if (document.activeElement !== quick && [...quick.options].some((o) => o.value === S.activeProfile)) quick.value = S.activeProfile;
  else if (document.activeElement !== quick && !S.activeProfile) quick.value = '';
  [...quick.options, ...list.options].forEach((o) => { o.textContent = o.value && o.value === S.activeProfile ? `● ${o.value}` : o.value; });
}

function syncV2(d) {
  const c = d.config;
  markActiveProfile(d.active_profile);
  setPressed($$('#styles .style-card'), (el) => el.dataset.style === c.screen_style);
  setPressed($$('#edges .chip'), (el) => !!c.edges_enabled[+el.dataset.edgeI]);
  // Only the sides that really have LEDs are offered (the LED count per side is set in Settings)
  const sideLeds = [c.leds_left, c.leds_top, c.leds_right, c.leds_bottom];
  $$('#edges .chip').forEach((el) => { el.hidden = !sideLeds[+el.dataset.edgeI]; });
  $('#edges-block').hidden = sideLeds.filter(Boolean).length < 2;
  sliders['sl-boost'].set(c.audio_boost);
  [sliders['sl-wb-r'], sliders['sl-wb-g'], sliders['sl-wb-b']].forEach((sl, i) => sl.set(c.white_balance[i]));
  sliders['sl-dark'].set(c.dark_light);
  sliders['sl-power'].set(c.max_power);
  sliders['sl-idle'].set(c.idle_off_minutes);
  $('#sw-dither').checked = c.dithering; $('#sw-adapt').checked = c.adaptive_smoothing; $('#sw-trans').checked = c.transitions;
  $('#sw-hotkeys').checked = c.hotkeys_enabled; $('#sw-notify').checked = c.notifications; $('#sw-rules').checked = c.app_rules_enabled;
  $('#monitor-color-note').textContent = c.monitor_color_profile_saved
    ? 'Hay una calibración guardada para este monitor. Se restaura al cambiar a él.'
    : 'Guarda gamma, saturación, blancos y la compensación HDR para el monitor actual.';
  const active = S.effects.find((e) => e.key === (d.active_effect || S.effect));
  if (active && Date.now() - S.fxTouched > 2500 && d.active_effect === S.effect) active.colors = c.effect_colors;
  fillFxColors(false);
  $('#sw-reverse').checked = c.effect_reverse; $('#sw-mirror').checked = c.effect_mirror;
  if (S.effects.length) { markEffect(); fxColorsCard(); }
  $$('#rules-list .item').forEach((el) => el.classList.toggle('active', el.dataset.exe === d.active_rule));
}

function renderMeasurement(measurement) {
  const m = measurement || {};
  const active = !!m.active;
  $('#measurement-scenario').disabled = active;
  $('#measurement-start').disabled = active;
  $('#measurement-stop').disabled = !active;
  $('#measurement-export').disabled = active || !m.report_ready;
  if (active) {
    $('#measurement-scenario').value = m.scenario || 'desktop';
    $('#measurement-status').textContent = `Midiendo ${m.scenario === 'video' ? 'vídeo' : m.scenario === 'game' ? 'juego' : 'escritorio'}: ${Math.round(m.duration_s || 0)} s · ${m.samples || 0} muestras.`;
  } else if (m.report_ready) {
    $('#measurement-status').textContent = 'Medición terminada; ya puedes exportar el informe.';
  } else {
    $('#measurement-status').textContent = 'Sin medición en curso.';
  }
}

async function refresh() {
  const d = await get('/api/status');
  if (!d) { setOnline(false); return; }
  setOnline(true);
  applyStatus(d);
}

async function loadPoints() {
  const d = await get('/api/led_points');
  if (d) {
    S.points = d.led_points;
    S.previewDirty = true;
    schedulePreview();
    if (S.editing) buildEditor();
  }
}
