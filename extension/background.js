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

/* ------------------------- capture Crowdfunding (Bricks/LPB) ------------------ */
// Stocke les captures dans chrome.storage.local ; l'envoi passe par
// POST {url}/api/cf/sync/ingest avec un jeton de portée « crowdfund »
// (jamais le jeton de valorisation — scopes distincts côté serveur).
const CF_HOSTS = ['app.bricks.co', 'app-legacy.bricks.co', 'app.lapremierebrique.fr'];
const CF_MAX = 800;

function cfPatterns() {
  return CF_HOSTS.flatMap(h => [`https://${h}/*`, `http://${h}/*`]);
}

async function readCf() {
  const s = await chrome.storage.local.get({ cfEnabled: false, cfToken: '', cfCaptures: [], cfDiag: {} });
  return {
    enabled: !!s.cfEnabled,
    token: s.cfToken || '',
    captures: s.cfCaptures || [],
    diag: s.cfDiag || { injected: [], jsonSeen: 0, lastUrl: '', lastSendAt: '', lastError: '' },
  };
}

async function syncCfScript() {
  const { enabled } = await readCf();
  let have = [];
  try { have = await chrome.scripting.getRegisteredContentScripts(); } catch { have = []; }
  const has = have.some(s => s.id === 'patcf-main');
  if (enabled && !has) {
    try {
      await chrome.scripting.registerContentScripts([{
        id: 'patcf-main', js: ['content-cf.js'],
        matches: cfPatterns(), runAt: 'document_start', allFrames: true,
      }]);
    } catch (e) { console.warn('register patcf', e); }
  } else if (!enabled && has) {
    try { await chrome.scripting.unregisterContentScripts({ ids: ['patcf-main'] }); } catch {}
  }
}

chrome.runtime.onInstalled.addListener(() => { syncCfScript(); });
chrome.runtime.onStartup.addListener(() => { syncCfScript(); });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.cfEnabled) syncCfScript();
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === 'pat-cf-sync') { syncCfScript().then(() => sendResponse({ ok: true })); return true; }
  if (msg.type === 'pat-cf-capture') {
    (async () => {
      const cf = await readCf();
      if (!cf.enabled) { sendResponse({ ok: false, error: 'disabled' }); return; }
      const p = msg.payload || {};
      cf.captures.push({ url: String(p.url || '').slice(0, 600), body: p.body, status: p.status, platform: p.platform, ts: p.ts });
      const dedup = new Map();
      for (const c of cf.captures) dedup.set(c.url, c);
      const list = [...dedup.values()].slice(-CF_MAX);
      cf.diag.jsonSeen = (cf.diag.jsonSeen || 0) + 1;
      cf.diag.lastUrl = String(p.url || '').slice(0, 200);
      await chrome.storage.local.set({ cfCaptures: list, cfDiag: cf.diag });
      sendResponse({ ok: true, n: list.length });
    })();
    return true;
  }
  if (msg.type === 'pat-cf-injected') {
    (async () => {
      const cf = await readCf();
      const p = msg.payload || {};
      const entry = { url: (p.url || '').slice(0, 200), platform: p.platform || '', at: new Date().toLocaleTimeString('fr-FR') };
      const arr = cf.diag.injected || [];
      if (!arr.some(e => e.url === entry.url)) arr.push(entry);
      cf.diag.injected = arr.slice(-10);
      await chrome.storage.local.set({ cfDiag: cf.diag });
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (msg.type === 'pat-cf-send') {
    (async () => {
      const cf = await readCf();
      const cfg = await chrome.storage.local.get({ url: '' });
      const base = (cfg.url || '').replace(/\/+$/, '');
      if (!cf.captures.length) { sendResponse({ ok: false, error: 'Aucune capture. Naviguez sur Bricks/LPB (F5) puis relancez.', diag: cf.diag }); return; }
      if (!/^https?:\/\//.test(base)) { sendResponse({ ok: false, error: 'URL de l’instance manquante (Options).' }); return; }
      if (!cf.token) { sendResponse({ ok: false, error: 'Jeton crowdfunding manquant (Options → Jeton Crowdfunding).' }); return; }
      try {
        const res = await fetch(base + '/api/cf/sync/ingest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cf.token },
          body: JSON.stringify({ captures: cf.captures }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        cf.diag.lastSendAt = new Date().toISOString();
        cf.diag.lastError = '';
        await chrome.storage.local.set({ cfCaptures: [], cfDiag: cf.diag });
        sendResponse({ ok: true, data });
      } catch (e) {
        cf.diag.lastError = String(e.message || e);
        await chrome.storage.local.set({ cfDiag: cf.diag });
        sendResponse({ ok: false, error: String(e.message || e) });
      }
    })();
    return true;
  }
  if (msg.type === 'pat-cf-state') {
    (async () => {
      const cf = await readCf();
      const cfg = await chrome.storage.local.get({ url: '' });
      sendResponse({ enabled: cf.enabled, hasToken: !!cf.token, captures: cf.captures.length, diag: cf.diag, server: cfg.url || '' });
    })();
    return true;
  }
  if (msg.type === 'pat-cf-clear') {
    (async () => {
      await chrome.storage.local.set({ cfCaptures: [], cfDiag: { injected: [], jsonSeen: 0, lastUrl: '', lastSendAt: '', lastError: '' } });
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (msg.type === 'pat-cf-tab') {
    (async () => {
      try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab || !tab.id) { sendResponse({ ok: false, error: 'Aucun onglet actif' }); return; }
        const kind = msg.action === 'force' ? 'pat-cf-force' : 'pat-cf-ping';
        const resp = await chrome.tabs.sendMessage(tab.id, { type: kind });
        sendResponse({ ok: true, tab: tab.url || '', info: resp });
      } catch (e) {
        sendResponse({ ok: false, error: 'Script non injecté sur cet onglet (rechargez la page) : ' + String(e.message || e) });
      }
    })();
    return true;
  }
});

/* ------------------------- capture manuelle (clic droit) ------------------ */
chrome.contextMenus.create({ id: 'capture-amount', title: '📥 Capturer vers Patrimony', contexts: ['selection'] });
chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== 'capture-amount') return;
  const host = info.pageUrl ? (() => { try { return new URL(info.pageUrl).host; } catch { return ''; } })() : '';
  chrome.storage.session.set({ capture: { amount: (info.selectionText || '').trim(), host, at: Date.now() } })
    .then(() => { if (chrome.action.openPopup) chrome.action.openPopup().catch(() => {}); });
});
