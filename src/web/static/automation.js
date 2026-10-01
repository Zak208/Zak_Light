'use strict';
/* Zak_light window - effects catalogue, automation, wizard, backup. Classic script: shares globals with the other files (load order in index.html). */
// ---------------------------------------------------------------- automation, wizard, backup
const setCfg = (body) => post('/api/settings', body);

function listItem(html, onRemove, exe) {
  const el = document.createElement('div');
  el.className = 'item'; if (exe) el.dataset.exe = exe;
  el.innerHTML = '<span></span><button type="button" aria-label="Quitar" title="Quitar">×</button>';
  $('span', el).innerHTML = html;
  $('button', el).onclick = onRemove;
  return el;
}
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function renderRules() {
  const box = $('#rules-list');
  box.textContent = '';
  if (!S.rules.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = 'Aún no hay reglas.'; box.appendChild(p); }
  S.rules.forEach((r, i) => {
    const what = [r.mode && (MODES[r.mode] || 'Apagar'), r.profile && `perfil “${r.profile}”`].filter(Boolean).join(' + ') || 'sin cambios';
    box.appendChild(listItem(`<b>${esc(r.exe)}</b> → ${esc(what)}`, () => { S.rules.splice(i, 1); saveAutomation(); }, r.exe));
  });
}
function renderSchedules() {
  const box = $('#sched-list');
  box.textContent = '';
  if (!S.schedules.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = 'Aún no hay horarios.'; box.appendChild(p); }
  S.schedules.forEach((s, i) => {
    const days = s.days.length === 7 ? 'cada día' : s.days.map((d) => DAYS[d]).join(' ');
    const val = s.value ? ` ${esc(s.value)}${s.action === 'brightness' ? '%' : s.action === 'sunrise' ? ' min' : ''}` : '';
    box.appendChild(listItem(`<b>${esc(s.time)}</b> ${esc(SCHED_LABEL[s.action] || s.action)}${val} · ${days}`, () => { S.schedules.splice(i, 1); saveAutomation(); }));
  });
}
async function saveAutomation() {
  const r = await post('/api/automation', { app_rules: S.rules, schedules: S.schedules });
  if (r) { S.rules = r.app_rules; S.schedules = r.schedules; }
  renderRules(); renderSchedules();
}
async function loadAutomation() {
  const d = await get('/api/automation');
  if (!d) return;
  S.rules = d.app_rules; S.schedules = d.schedules;
  renderRules(); renderSchedules();
  const ul = $('#hk-list'); ul.textContent = '';
  (d.hotkeys || []).forEach((h) => { const li = document.createElement('li'); li.textContent = h.text + (h.active ? '' : ' (no disponible)'); if (!h.active || !d.hotkeys_enabled) li.className = 'off'; ul.appendChild(li); });
  loadApps();
}
async function loadApps() {
  const d = await get('/api/windows');
  const sel = $('#rule-app');
  sel.textContent = '';
  (d ? d.apps : []).forEach((a) => { const o = document.createElement('option'); o.value = a.exe; o.textContent = `${a.exe} — ${a.title.slice(0, 34)}`; sel.appendChild(o); });
  if (!sel.options.length) { const o = document.createElement('option'); o.value = ''; o.textContent = '(abre el programa y pulsa ↻)'; sel.appendChild(o); }
  const prof = $('#rule-profile');
  prof.textContent = '';
  const none = document.createElement('option'); none.value = ''; none.textContent = 'Sin cambiar perfil'; prof.appendChild(none);
  S.profiles.forEach((n) => { const o = document.createElement('option'); o.value = n; o.textContent = n; prof.appendChild(o); });
}

function bindAutomation() {
  const days = $('#sch-days');
  DAYS.forEach((d, i) => {
    const b = document.createElement('button');
    b.className = 'chip'; b.textContent = d; b.setAttribute('aria-pressed', 'true');
    b.onclick = () => {
      S.schDays = S.schDays.includes(i) ? S.schDays.filter((x) => x !== i) : [...S.schDays, i].sort();
      b.setAttribute('aria-pressed', String(S.schDays.includes(i)));
    };
    days.appendChild(b);
  });
  const hint = () => {
    const a = $('#sch-action').value;
    $('#sch-value').hidden = a === 'on' || a === 'off';
    $('#sch-value').placeholder = { mode: 'screen / rhythm / effect / static', profile: 'nombre del perfil', brightness: '0-100', sunrise: 'minutos (15)' }[a] || '';
    $('#sch-hint').textContent = a === 'sunrise' ? 'Sube la luz en ese tiempo hasta la hora indicada.' : '';
  };
  $('#sch-action').onchange = hint; hint();
  $('#rule-refresh').onclick = loadApps;
  $('#rule-add').onclick = () => {
    const exe = $('#rule-app').value;
    if (!exe) return toast('Elige una aplicación de la lista', true);
    const mode = $('#rule-mode').value, profile = $('#rule-profile').value;
    if (!mode && !profile) return toast('Elige un modo o un perfil para la regla', true);
    S.rules = S.rules.filter((r) => r.exe !== exe.toLowerCase());
    S.rules.push({ exe: exe.toLowerCase(), mode, profile });
    saveAutomation();
  };
  $('#sch-add').onclick = () => {
    const time = $('#sch-time').value.trim(), action = $('#sch-action').value, value = $('#sch-value').value.trim();
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) return toast('La hora debe ser HH:MM, por ejemplo 07:30', true);
    if (!S.schDays.length) return toast('Elige al menos un día', true);
    if (['mode', 'profile', 'brightness'].includes(action) && !value) return toast('Falta el valor de la acción', true);
    S.schedules.push({ time, days: S.schDays, action, value: ['on', 'off'].includes(action) ? '' : value });
    saveAutomation();
  };
  $('#sw-rules').onchange = (e) => post('/api/automation', { app_rules_enabled: e.target.checked });
  $('#sw-hotkeys').onchange = async (e) => { await setCfg({ hotkeys_enabled: e.target.checked }); setTimeout(loadAutomation, 400); };
  $('#sw-notify').onchange = (e) => setCfg({ notifications: e.target.checked });
  slider('sl-autoclose', { out: 'v-autoclose', fmt: (v) => (v ? v + ' min' : 'Nunca'), onInput: (v) => post('/api/startup', { ui_autoclose_minutes: v }) });
  slider('sl-idle', { out: 'v-idle', fmt: (v) => (v ? v + ' min' : 'Nunca'), onInput: (v) => setCfg({ idle_off_minutes: v }) });
}

// ---- setup wizard: which colour is each wire, and where does the strip start
const WIZ = { colors: [], step: 0 };
function wizShow(text, choices) {
  $('#wiz-start').hidden = true; $('#wiz-step').hidden = false;
  $('#wiz-text').textContent = text;
  const box = $('#wiz-choices'); box.textContent = '';
  choices.forEach(([label, fn, css]) => { const b = document.createElement('button'); b.className = 'btn small'; if (css) b.style.cssText = css; b.textContent = label; b.onclick = fn; box.appendChild(b); });
}
async function wizChannel() {
  S.modeTouched = Date.now(); S.mode = 'calibrate'; renderMode();
  await post('/api/calibrate_action', { action: 'test_channel', index: WIZ.step });
  const ask = (letter) => () => { WIZ.colors.push(letter); WIZ.step++; WIZ.step < 3 ? wizChannel() : wizWire(); };
  wizShow(`Paso ${WIZ.step + 1} de 3: ¿de qué color se ha encendido la tira?`, [
    ['Rojo', ask('R'), 'border-color:#ef4444'], ['Verde', ask('G'), 'border-color:#22c55e'], ['Azul', ask('B'), 'border-color:#3b82f6'],
    ['No se enciende', wizCancel],
  ]);
}
async function wizWire() {
  const map = WIZ.colors.join('');
  if (!WIRES.includes(map)) { toast('Las respuestas no cuadran (un color repetido). Repite el asistente.', true); return wizCancel(); }
  await post('/api/settings', { wire_map: map });
  wizLed();
}
async function wizLed() {
  await post('/api/calibrate_action', { action: 'test_led', index: 0 });
  wizShow(`Orden de colores guardado (${WIZ.colors.join('')}). Ahora se ha encendido un solo LED, el primero de la tira. ¿Dónde está?`, [
    ['Abajo a la izquierda', () => wizDone('bl')], ['Arriba a la izquierda', () => wizDone('tl')],
    ['Arriba a la derecha', () => wizDone('tr')], ['Abajo a la derecha', () => wizDone('br')],
  ]);
}
const cornerOf = (pt) => (pt[1] < 0.5 ? 't' : 'b') + (pt[0] < 0.5 ? 'l' : 'r');
async function wizDone(corner) {
  const first = S.points[0] && cornerOf(S.points[0]), last = S.points.length && cornerOf(S.points[S.points.length - 1]);
  let msg = 'Tira configurada';
  if (corner !== first) {
    if (corner === last) { const r = await post('/api/auto_layout', { mode: 'reverse' }); if (r) S.points = r.led_points; msg = 'Sentido de la tira corregido'; }
    else msg = 'El primer LED está en un sitio distinto a los extremos de las zonas: usa “Editor avanzado” para colocarlos';
  }
  await wizCancel(true);
  toast(msg);
  post('/api/settings', { onboarded: true });
}
async function wizCancel(done) {
  await post('/api/calibrate_action', { action: 'stop' });
  S.modeTouched = 0; WIZ.colors = []; WIZ.step = 0;
  $('#wiz-start').hidden = false; $('#wiz-step').hidden = true;
  refresh();
}

// ---- backup
async function exportSettings() {
  const d = await get('/api/export');
  if (!d) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' }));
  a.download = `zak_light_${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
async function exportDiagnostics() {
  const d = await get('/api/diagnostics/export');
  if (!d) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' }));
  a.download = `zak_light_diagnostico_${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast('Informe de diagnóstico exportado');
}
async function exportPerformanceMeasurement() {
  const d = await get('/api/performance_measurement/export');
  if (!d) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' }));
  a.download = `zak_light_medicion_${d.scenario || 'rendimiento'}_${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast('Medición de rendimiento exportada');
}
async function importSettings(file) {
  if (!file) return;
  try {
    const d = JSON.parse(await file.text());
    if (d.format !== 'zak_light_backup') throw new Error('format');
    if (!confirm('¿Restaurar esta copia? Reemplaza tus ajustes actuales y añade sus perfiles.')) return;
    const r = await post('/api/import', { config: d.config, profiles: d.profiles });
    if (r) { toast(`Copia restaurada (${r.profiles} perfiles)`); refresh(); loadProfiles(); loadAutomation(); }
  } catch { toast('Ese archivo no es una copia de Zak_light', true); }
}
