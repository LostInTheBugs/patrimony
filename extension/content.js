// Patrimony Capture — content script injecté dynamiquement par mapping.
// Lit le 1er élément correspondant au sélecteur CSS du mapping et l'envoie
// au background. Ne lit RIEN d'autre ; aucune donnée n'est stockée ici.
(() => {
  if (window.__patCapContent) return; // un seul injecteur par page
  window.__patCapContent = true;
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === 'pat-read') {
      try {
        const el = document.querySelector(msg.selector);
        if (!el) { sendResponse({ ok: false, error: 'selector_missing' }); return; }
        let text = (el.textContent || '').replace(/\s+/g, ' ').trim();
        // certains sites affichent le montant dans un attribut (aria-label…)
        if (!text && el.getAttribute) {
          text = (el.getAttribute('aria-label') || el.getAttribute('data-value') || '').trim();
        }
        sendResponse({ ok: true, text, url: location.href });
      } catch (e) {
        sendResponse({ ok: false, error: String(e) });
      }
    }
    return true; // canal asynchrone
  });
})();
