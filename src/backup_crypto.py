"""Sauvegarde chiffrée — enveloppe JSON auto-descriptive en base64.

Format (versionné, format de restauration future garanti) :
    {"app": "patrimony", "what": "backup-encrypted", "version": 1,
     "kdf": {"algo": "pbkdf2-hmac-sha256", "iterations": 310000, "salt": b64},
     "aead": {"algo": "aes-256-gcm", "nonce": b64}, "ct": b64}

- Chiffrement AES-256-GCM (authentifié : toute altération ou mauvais mot de
  passe échoue au déchiffrement, sans oracle).
- Clé dérivée PBKDF2-HMAC-SHA256 (310 000 itérations), sel aléatoire 16 o.
- Le mot de passe n'est jamais stocké ; les paramètres KDF/AEAD voyagent
  dans l'enveloppe (compatibilité de restauration même après un changement
  d'itérations).
"""
import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

APP_TAG = "patrimony"
WHAT = "backup-encrypted"
ENVELOPE_VERSION = 1
KDF_ITERS = 310_000


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def derive_key(passphrase: str, salt: bytes, iterations: int = KDF_ITERS) -> bytes:
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    ).derive(passphrase)


def encrypt_bytes(plain: bytes, passphrase: str) -> str:
    """Chiffre des octets → enveloppe base64 (une seule chaîne, fichier .pat.b64)."""
    salt, nonce = os.urandom(16), os.urandom(12)
    key = derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, plain, None)
    env = {
        "app": APP_TAG,
        "what": WHAT,
        "version": ENVELOPE_VERSION,
        "kdf": {"algo": "pbkdf2-hmac-sha256", "iterations": KDF_ITERS, "salt": _b64e(salt)},
        "aead": {"algo": "aes-256-gcm", "nonce": _b64e(nonce)},
        "ct": _b64e(ct),
    }
    return _b64e(json.dumps(env, separators=(",", ":")).encode("utf-8"))


def decrypt_bytes(envelope_b64: str, passphrase: str) -> bytes:
    """Déchiffre une enveloppe. Lève ValueError si le format est inconnu,
    le mot de passe invalide ou le contenu altéré."""
    try:
        env = json.loads(_b64d(envelope_b64.strip()))
    except Exception as e:
        raise ValueError("Enveloppe illisible (fichier corrompu ?)") from e
    if env.get("app") != APP_TAG or env.get("what") != WHAT or env.get("version") != ENVELOPE_VERSION:
        raise ValueError("Format de sauvegarde non reconnu")
    kdf, aead = env.get("kdf") or {}, env.get("aead") or {}
    if kdf.get("algo") != "pbkdf2-hmac-sha256" or aead.get("algo") != "aes-256-gcm":
        raise ValueError("Algorithme de sauvegarde non supporté")
    try:
        key = derive_key(passphrase, _b64d(kdf["salt"]), iterations=int(kdf["iterations"]))
        return AESGCM(key).decrypt(_b64d(aead["nonce"]), _b64d(env["ct"]), None)
    except Exception as e:
        raise ValueError("Mot de passe invalide ou fichier altéré") from e
