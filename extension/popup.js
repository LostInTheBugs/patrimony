// Patrimony Capture — popup
const $ = (id) => document.getElementById(id);

function cleanAmount(s) {
  let v = (s || '').replace(/\s/g, '').replace('€', '').replace('EUR', '').trim();
  if (v.startsWith('-') || v.startsWith('+')) v = v.slice(1);
  if (v.includes(',') && v.includes('.')) v = v.replace(/\./g, '');
  v = v.replace(',', '.');
  const n = parseFloat(v);
  return Number.isFinite(n) && n > 0 ? String(n) : '';
}

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso + 'T12:00:00').toLocaleDateString('fr-FR', { day: '2-digit', month: 'short' });
}

async function renderMaps(cfg, accounts) {
  const s = await chrome.storage.local.get({ mappings: [], autoLog: {} });
  const maps = (s.mappings || []).filter(m => m.active);
  const accName = id => { const a = accounts.find(x => x.id === id); return a ? a.name : ('#' + id); };
  if (!maps.length) return;
  $('mapsHead').style.display = 'block';
  $('mapsWrap').innerHTML = maps.map((m, i) => {
    const log = (s.autoLog || {})[m.id];
    const st = log ? (log.error ? '<span class="st err" title="' + esc(log.error) + '">⚠️ ' + fmtDate(log.date) + '</span>'
      : '<span class="st ok">✓ ' + fmtDate(log.date) + ' · ' + esc(log.value) + '</span>')
      : '<span class="st">—</span>';
    return '<div class="map"><b>' + esc(m.host) + '</b><span class="mono">' + esc(m.selector) + '</span>' +
      '<span class="muted" style="font-size:10.5px">→ ' + esc(accName(m.accountId)) + '</span>' +
      st +
      '<button title="Capturer maintenant (site ouvert)" onclick="testMap(' + i + ')">▶</button></div>';
  }).join('');
}
async function testMap(i) {
  const s = await chrome.storage.local.get({ mappings: [] });
  const res = await chrome.runtime.sendMessage({ type: 'pat-test', id: (s.mappings || [])[i].id }).catch(() => ({}));
  const rows = document.querySelectorAll('#mapsWrap .map');
  if (rows[i]) {
    const st = rows[i].querySelector('.st');
    st.className = 'st ' + (res && res.ok ? 'ok' : 'err');
    st.textContent = res && res.ok ? ('✓ ' + res.amount) : (res && res.error === 'tab_not_open' ? 'ouvrez le site' : '⚠️ ' + ((res && res.error) || 'erreur'));
  }
}

async function main() {
  const cfg = await chrome.storage.local.get({ url: '', token: '', note: '' });
  const base = cfg.url.replace(/\/+$/, '');
  const hasCfg = /^https?:\/\//.test(base) && cfg.token.length >= 20;
  $('cfgLink').onclick = () => chrome.runtime.openOptionsPage();
  if (!hasCfg) { $('noCfg').style.display = 'block'; return; }
  $('form').style.display = 'block';
  $('lblAcc').textContent = 'Actif — ' + new URL(base).host;

  const H = { 'Authorization': 'Bearer ' + cfg.token, 'Content-Type': 'application/json' };
  const cap = await chrome.storage.session.get('capture').then(r => r.capture || null).catch(() => null);
  let accs = [];
  try {
    const r = await fetch(base + '/api/accounts', { headers: H });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    accs = (d.accounts || []).filter(a => !a.close_date && a.active !== false && a.valuation_mode !== 'auto');
    if (!accs.length) throw new Error('aucun actif manuel disponible');
    $('pAcc').innerHTML = accs.map(a => '<option value="' + a.id + '">' + esc(a.name) + ' (' + (a.currency || 'EUR') + ')</option>').join('');
  } catch (e) {
    $('pStatus').className = 'err';
    $('pStatus').textContent = '❌ Connexion impossible : ' + e.message;
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  $('pDate').value = today;
  if (cap && cap.amount) {
    const cleaned = cleanAmount(cap.amount);
    $('pAmt').value = cleaned || cap.amount;
    $('pHint').textContent = cap.host ? 'Capturé depuis ' + cap.host : '';
  }
  $('pNote').value = cfg.note || '';
  if (cap && cap.host && !$('pNote').value.includes(cap.host)) {
    $('pNote').value = ($('pNote').value ? $('pNote').value + ' · ' : '') + cap.host;
  }
  $('pSave').onclick = async () => {
    const amt = parseFloat(($('pAmt').value || '').replace(',', '.'));
    if (!Number.isFinite(amt) || amt <= 0) { flash('Montant invalide', 'err'); return; }
    if (!$('pDate').value) { flash('Date requise', 'err'); return; }
    $('pSave').disabled = true;
    try {
      const r = await fetch(base + '/api/accounts/' + $('pAcc').value + '/valuation', {
        method: 'POST',
        headers: H,
        body: JSON.stringify({ value: amt, val_date: $('pDate').value, note: $('pNote').value.trim() }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      await chrome.storage.session.remove('capture');
      flash('✅ Valorisation enregistrée (' + amt + ' ' + (accs.find(x => x.id === Number($('pAcc').value)) || {}).currency + ')', 'ok');
      $('pSave').disabled = false;
    } catch (e) {
      $('pSave').disabled = false;
      flash('❌ ' + e.message, 'err');
    }
  };
  $('pAmt').focus();
  $('pAmt').select();
  renderMaps(cfg, accs);
}

function flash(msg, cls) { const s = $('pStatus'); s.className = cls; s.textContent = msg; }

main();
