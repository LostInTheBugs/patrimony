// Patrimony Capture — service worker (MV3)
// 1) clic droit sur une sélection → capture manuelle via le popup ;
// 2) mappings CSS (options) → content script dynamique → capture auto
//    quotidienne (1 envoi par mapping et par jour, valeur inchangée = rien).
const DAY = 864e5;

function cleanAmount(s) {
  let v = (s || '').replace(/\s/g, '').replace('€', '').replace('EUR', '').trim();
  if (v.startsWith('-') || v.startsWith('+')) v = v.slice(1);
  if (v.includes(',') && v.includes('.')) v = v.replace(/\./g, '');
  v = v.replace(',', '.');
  const n = parseFloat(v);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function patternOf(host) {
  const h = host.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '').replace(/^\*\./, '');
  return [`https://${h}/*`, `https://*.${h}/*`, `http://${h}/*`, `http://*.${h}/*`];
}
function mappingMatches(m, url) {
  try { return new URL(url).hostname === m.host || new URL(url).hostname.endsWith('.' + m.host); }
  catch { return false; }
}

/* ------------------------- mappings automatiques ------------------------- */
async function readMappings() {
  const s = await chrome.storage.local.get({ mappings: [], autoLog: {} });
  return { mappings: s.mappings || [], autoLog: s.autoLog || {} };
}

async function syncRegistered() {
  // état attendu vs scripts enregistrés → inscrit/retire les différences
  const { mappings } = await readMappings();
  const want = new Map(mappings.filter(m => m.active).map(m => ['patmap-' + m.id, m]));
  let have = [];
  try { have = await chrome.scripting.getRegisteredContentScripts(); } catch { have = []; }
  const haveIds = new Set(have.map(s => s.id));
  for (const [id, m] of want) {
    if (haveIds.has(id)) continue;
    try {
      await chrome.scripting.registerContentScripts([{
        id, js: ['content.js'],
        matches: patternOf(m.host),
        runAt: 'document_idle', allFrames: false,
      }]);
    } catch (e) { console.warn('register', id, e); }
  }
  for (const s of have) {
    if (!want.has(s.id)) { try { await chrome.scripting.unregisterContentScripts({ ids: [s.id] }); } catch {} }
  }
}

async function pushValuation(m, text, manual = false) {
  const cfg = await chrome.storage.local.get({ url: '', token: '', note: '' });
  const base = cfg.url.replace(/\/+$/, '');
  if (!/^https?:\/\//.test(base) || !cfg.token) return { ok: false, error: 'config' };
  const amount = cleanAmount(text);
  if (amount === null) return { ok: false, error: 'amount_unreadable' };
  const r = await fetch(base + '/api/accounts/' + m.accountId + '/valuation', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + cfg.token, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      value: amount,
      val_date: new Date().toISOString().slice(0, 10),
      note: ((cfg.note || 'capture auto') + ' · ' + m.host + ' · ' + m.selector).slice(0, 200),
    }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) return { ok: false, error: d.detail || ('HTTP ' + r.status) };
  return { ok: true, amount };
}

async function processMapping(m, text, url, force) {
  const { autoLog } = await readMappings();
  const log = autoLog[m.id] || {};
  const today = new Date().toISOString().slice(0, 10);
  const val = cleanAmount(text);
  if (!force && val === null) return { ok: false, error: 'amount_unreadable', log };
  if (!force && log.date === today && log.value === String(val)) return { ok: false, error: 'duplicate', log };
  const res = await pushValuation(m, text, force);
  if (res.ok) {
    const next = { ...autoLog, [m.id]: { date: today, value: String(res.amount), at: Date.now() } };
    await chrome.storage.local.set({ autoLog: next });
    res.log = next[m.id];
  }
  return res;
}

chrome.runtime.onInstalled.addListener(() => { syncRegistered(); });
chrome.runtime.onStartup.addListener(() => { syncRegistered(); });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.mappings) syncRegistered();
});

// message d'un content script injecté (document_idle)
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === 'pat-sync') { syncRegistered().then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === 'pat-test') {
    (async () => {
      const { mappings } = await readMappings();
      const m = mappings.find(x => x.id === msg.id);
      if (!m) return sendResponse({ ok: false, error: 'no_mapping' });
      const res = await captureNow(m);
      sendResponse(res);
    })();
    return true;
  }
  if (msg.type !== 'pat-read') return;
  (async () => {
    const { mappings } = await readMappings();
    const m = mappings.find(x => x.active && mappingMatches(x, sender.url || '') &&
      x.selector === msg.selector);
    if (!m) return sendResponse({ ok: false, error: 'no_mapping' });
    const res = await processMapping(m, msg.text, sender.url, false);
    sendResponse({ ok: res.ok, error: res.error, log: res.log });
  })();
  return true;
});

// action « capturer maintenant » depuis le popup (onglet ouvert du site)
async function captureNow(m) {
  const tabs = await chrome.tabs.query({ url: patternOf(m.host) });
  if (!tabs.length) return { ok: false, error: 'tab_not_open' };
  const tab = tabs[0];
  const inj = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (sel) => {
      const el = document.querySelector(sel);
      if (!el) return { text: '' };
      let t = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (!t) t = (el.getAttribute('aria-label') || '').trim();
      return { text: t };
    },
    args: [m.selector],
  }).catch(e => ({ error: String(e) }));
  if (!inj || inj.error || !(inj[0] && inj[0].result && inj[0].result.text)) {
    return { ok: false, error: 'selector_missing' };
  }
  return processMapping(m, inj[0].result.text, tab.url, true);
}

/* ------------------------- capture manuelle (clic droit) ------------------ */
chrome.contextMenus.create({ id: 'capture-amount', title: '📥 Capturer vers Patrimony', contexts: ['selection'] });
chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== 'capture-amount') return;
  const host = info.pageUrl ? (() => { try { return new URL(info.pageUrl).host; } catch { return ''; } })() : '';
  chrome.storage.session.set({ capture: { amount: (info.selectionText || '').trim(), host, at: Date.now() } })
    .then(() => { if (chrome.action.openPopup) chrome.action.openPopup().catch(() => {}); });
});
