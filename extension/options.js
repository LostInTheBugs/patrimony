// Patrimony Capture — options (connexion + mappings CSS)
const $ = (id) => document.getElementById(id);
let ACCOUNTS = [];

function normalizeUrl(raw) {
  let u;
  try { u = new URL(raw.trim()); } catch { return null; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  u.hash = ''; u.pathname = ''; u.search = '';
  return u.origin;
}
function setStatus(msg, cls, el) { const s = el || $('status'); s.className = cls || ''; s.textContent = msg; }
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

async function load() {
  const cfg = await chrome.storage.local.get({ url: '', token: '', note: '', mappings: [], autoLog: {} });
  $('oUrl').value = cfg.url;
  $('oToken').value = cfg.token;
  $('oNote').value = cfg.note || '';
  renderMappings(cfg.mappings, cfg.autoLog);
  refreshAccounts();
}
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso + 'T12:00:00').toLocaleDateString('fr-FR', { day: '2-digit', month: 'short' });
}

async function refreshAccounts() {
  const origin = normalizeUrl($('oUrl').value);
  const token = $('oToken').value.trim();
  if (!origin || !token) return;
  try {
    const r = await fetch(origin + '/api/accounts', { headers: { 'Authorization': 'Bearer ' + token } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    ACCOUNTS = (d.accounts || []).filter(a => a.valuation_mode !== 'auto' && !a.close_date);
    const sel = $('mAcc');
    sel.innerHTML = ACCOUNTS.map(a => '<option value="' + a.id + '">' + esc(a.name) + ' (' + (a.currency || 'EUR') + ')</option>').join('');
    if (!ACCOUNTS.length) sel.innerHTML = '<option value="">— aucun actif manuel —</option>';
  } catch (e) { /* silencieux : le test de connexion dira l'erreur */ }
}

async function getMappings() {
  return (await chrome.storage.local.get({ mappings: [], autoLog: {} }));
}
async function setMappings(mappings) {
  await chrome.storage.local.set({ mappings });
  renderMappings(mappings, (await chrome.storage.local.get('autoLog')).autoLog || {});
}

function renderMappings(mappings, autoLog) {
  const body = $('mapBody');
  if (!mappings.length) {
    body.innerHTML = '<tr><td colspan="5" class="note">Aucun mapping — ajoutez-en un ci-dessous.</td></tr>';
    return;
  }
  body.innerHTML = mappings.map((m, i) => {
    const log = autoLog[m.id];
    const st = log
      ? (log.error ? '<span class="st err">⚠️ ' + esc(log.error) + '</span>'
          : '<span class="st ok">✓ ' + fmtDate(log.date) + ' · ' + esc(log.value || '') + '</span>')
      : '<span class="st">jamais</span>';
    const acc = ACCOUNTS.find(a => a.id === m.accountId);
    return '<tr>' +
      '<td>' + esc(m.host) + (m.active ? '' : ' <span class="st">(off)</span>') + '</td>' +
      '<td class="mono">' + esc(m.selector) + '</td>' +
      '<td>' + esc(acc ? acc.name : ('#' + m.accountId)) + '</td>' +
      '<td>' + st + '</td>' +
      '<td style="white-space:nowrap">' +
        '<button class="mini" style="color:#3fb950;border-color:#3fb95033" title="Capturer maintenant (site ouvert requis)" onclick="testMap(' + i + ')">▶</button> ' +
        '<button class="mini" style="color:#e6edf3;border-color:#555" title="Éditer" onclick="editMap(' + i + ')">✎</button> ' +
        '<button class="mini" title="Supprimer" onclick="delMap(' + i + ')">✕</button></td></tr>';
  }).join('');
}

async function persistAndSync() {
  const { mappings } = await getMappings();
  await chrome.storage.local.set({ mappings });
  chrome.runtime.sendMessage({ type: 'pat-sync' }).catch(() => {});
  renderMappings(mappings, (await chrome.storage.local.get('autoLog')).autoLog || {});
}

async function requestHost(host) {
  const h = host.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
  const origins = [`https://${h}/*`, `https://*.${h}/*`, `http://${h}/*`, `http://*.${h}/*`];
  return chrome.permissions.request({ origins });
}

$('mAdd').onclick = async () => {
  const host = $('mHost').value.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '');
  const selector = $('mSel').value.trim();
  const accountId = Number($('mAcc').value || 0);
  if (!host || !selector) { setStatus('⚠️ Site et sélecteur requis', 'err'); return; }
  if (!accountId) { setStatus('⚠️ Actif cible requis (connexion enregistrée ?)', 'err'); return; }
  if (!(await requestHost(host))) { setStatus('⚠️ Autorisation d\'accès à ' + host + ' refusée — mapping non ajouté.', 'err'); return; }
  const { mappings } = await getMappings();
  mappings.push({ id: uid(), host, selector, accountId, active: true });
  await setMappings(mappings);
  $('mHost').value = ''; $('mSel').value = '';
  setStatus('✅ Mapping ajouté — enregistrez la connexion si ce n\'est pas fait.', 'ok');
  chrome.runtime.sendMessage({ type: 'pat-sync' }).catch(() => {});
};

async function editMap(i) {
  const { mappings } = await getMappings();
  const m = mappings[i];
  const host = prompt('Site (hôte)', m.host);
  if (host === null) return;
  const selector = prompt('Sélecteur CSS', m.selector);
  if (selector === null) return;
  m.host = host.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '');
  m.selector = selector.trim();
  await setMappings(mappings);
  chrome.runtime.sendMessage({ type: 'pat-sync' }).catch(() => {});
}

async function delMap(i) {
  const { mappings } = await getMappings();
  if (!confirm('Supprimer ce mapping ?')) return;
  mappings.splice(i, 1);
  await setMappings(mappings);
  chrome.runtime.sendMessage({ type: 'pat-sync' }).catch(() => {});
}

async function testMap(i) {
  const { mappings } = await getMappings();
  const res = await chrome.runtime.sendMessage({ type: 'pat-test', id: mappings[i].id }).catch(() => ({}));
  const statuses = document.querySelectorAll('#mapBody tr');
  const st = statuses[i] ? statuses[i].querySelector('.st') : null;
  if (st) {
    st.className = res && res.ok ? 'st ok' : 'st err';
    st.textContent = res && res.ok
      ? '✓ envoyé (' + res.amount + ')'
      : (res && res.error === 'tab_not_open' ? '⚠️ ouvrez le site d\'abord' : '⚠️ ' + ((res && res.error) || 'erreur'));
  }
}

$('oTest').onclick = async () => {
  const origin = normalizeUrl($('oUrl').value);
  if (!origin) { setStatus('❌ URL invalide (http/https requise)', 'err'); return; }
  if (($('oToken').value || '').length < 20) { setStatus('❌ Jeton manquant ou trop court', 'err'); return; }
  setStatus('Test en cours…');
  try {
    const r = await fetch(origin + '/api/accounts', { headers: { 'Authorization': 'Bearer ' + $('oToken').value.trim() } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const n = (d.accounts || []).filter(a => a.valuation_mode !== 'auto').length;
    setStatus('✅ Connexion OK — ' + n + ' actif(s) manuel(s) capturables.', 'ok');
    await refreshAccounts();
  } catch (e) {
    setStatus('❌ Échec : ' + e.message + ' (URL correcte ? jeton valide ? instance joignable ?)', 'err');
  }
};

$('oSave').onclick = async () => {
  const origin = normalizeUrl($('oUrl').value);
  if (!origin) { setStatus('❌ URL invalide', 'err'); return; }
  await chrome.storage.local.set({
    url: origin,
    token: $('oToken').value.trim(),
    note: $('oNote').value.trim().slice(0, 100),
  });
  let permOk = true;
  try { await chrome.permissions.request({ origins: [origin + '/*'] }); } catch { permOk = false; }
  setStatus(permOk
    ? '✅ Enregistré — autorisation d\'accès à ' + origin + ' accordée.'
    : '⚠️ Enregistré, mais autorisation d\'accès refusée : l\'envoi échouera.', 'ok');
  refreshAccounts();
};

load();
