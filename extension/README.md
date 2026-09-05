# Patrimony Capture (extension navigateur)

Extension Chrome/Edge MV3 — **passive et sans identifiants** : vous
sélectionnez un montant sur n'importe quelle page (votre banque, votre
courtier, Bricks.co…) et l'extension envoie une **valorisation** à votre
instance Patrimony. Aucune donnée ne quitte votre réseau ; l'extension ne
lit que ce que vous sélectionnez.

## Installation (mode développeur)

1. `chrome://extensions` → activer **Mode développeur**
2. **Charger l'extension non empaquetée** → sélectionner ce dossier
   (`extension/`)
3. Cliquer sur l'icône 🐴 → ⚙️ → renseigner :
   - **URL de l'instance** : `http://192.0.2.10:8020` (LAN) ou votre
     domaine HTTPS
   - **Jeton API** : dans Patrimony → Paramètres → **Accès API
     (extension)** → « Nouveau jeton » → copier (affiché une seule fois)
   - « Tester la connexion » puis « Enregistrer » (l'extension demande
     l'autorisation d'accès à cette URL)

## Usage

1. Sur n'importe quelle page, **sélectionner un montant** (ex. solde du
   PEA : `12 345,67 €`)
2. Clic droit → **📥 Capturer vers Patrimony**
3. Dans le popup : choisir l'**actif** cible, vérifier montant/date/note →
   **Enregistrer**

La valorisation apparaît dans Patrimony (source manuelle, note incluant le
site d'origine), avec la date du jour. Les actifs en mode automatique
(cours) sont exclus de la liste.

## Captures automatiques (mappings CSS) — v1.1.0

Dans les options, section « Captures automatiques » : ajoutez un mapping
par site — hôte (`boursorama.com`), **sélecteur CSS** du montant (clic
droit → Inspecter sur la valeur → copier le sélecteur), et l'actif cible.

- L'extension injecte un content script **uniquement sur les hôtes
  mappés** (`chrome.scripting.registerContentScripts` dynamique) ; il lit
  le **1er élément** correspondant, rien d'autre.
- Envoi **1 fois par jour max** par mapping, **seulement si la valeur a
  changé** (le site doit être ouvert à un moment de la journée).
- ▶ sur une ligne = capturer maintenant (site ouvert requis) ; ✎ =
  éditer ; ✕ = supprimer (le script est retiré du site).
- Chaque mapping demande l'autorisation d'accès au site (optionnelle,
  révocable dans chrome://extensions → détails du site).
- Les résultats récents s'affichent dans le popup et dans les options.

## Sécurité

- **Jeton API** : stocké uniquement dans `chrome.storage.local` (votre
  navigateur) ; sur le serveur il n'existe que **haché** (SHA-256), est
  affiché une seule fois à la création et peut être **révoqué** à tout
  moment (Paramètres → Accès API). Expiration conseillée : 90 jours.
- **Interdit aux comptes protégés** : un coffre exige une session
  interactive — un jeton ne peut donc jamais ouvrir un coffre.
- Permission d'hôte **optionnelle** : demandée pour l'URL de *votre*
  instance uniquement, jamais `<all_urls>`.
- L'API de Patrimony n'est jamais mise en cache par son service worker ;
  l'extension n'a pas d'accès aux pages (pas de content script en v1).

## Développement

- `background.js` : menu contextuel (sélection → `chrome.storage.session`
  → ouverture du popup, Chrome ≥ 127) ; mappings : enregistrement
  dynamique des content scripts, réconciliation au démarrage,
  throttling quotidien + dédup par valeur, capture forcée
  (`chrome.scripting.executeScript`)
- `content.js` : injecté par mapping — lit le 1er élément du sélecteur
  (texte, repli `aria-label`/`data-value`), message `pat-read`
- `popup.js` : liste des actifs via `GET /api/accounts`, envoi
  `POST /api/accounts/{id}/valuation`, état des mappings + ▶
- `options.js` : configuration + test + `chrome.permissions.request` +
  CRUD des mappings (hôte, sélecteur CSS, actif cible)
- Icônes générées depuis `public/logo-mark.png` (Pillow)

Charger l'extension → toute modification de `background.js`/
`manifest.json` exige « Recharger » sur `chrome://extensions`.
