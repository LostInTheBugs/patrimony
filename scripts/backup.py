#!/usr/bin/env python3
"""Sauvegarde chiffrée niveau OPS — chiffre/déchiffre un FICHIER quelconque
(typiquement un dump SQLite complet de Patrimony) avec l'enveloppe
AES-256-GCM + PBKDF2 de src/backup_crypto.py.

Usage :
    PATRIMONY_BACKUP_PASS='...' python scripts/backup.py encrypt <entree> <sortie.pat.b64>
    PATRIMONY_BACKUP_PASS='...' python scripts/backup.py decrypt <entree.pat.b64> <sortie>

Sans variable d'environnement, le mot de passe est demandé en interactif
(getpass, pas d'écho). Le chiffrement est authentifié : fichier altéré ou
mauvais mot de passe → échec propre.

Runbook de restauration (prod LAN) :
    # sauvegarde : dump SQLite à chaud + chiffrement
    ssh cpt-claude@<hote> 'docker exec patrimony python -c "import sqlite3,sys;\
        s=sqlite3.connect(\"data/app.db\"); d=sqlite3.connect(\"/tmp/app.db\");\
        s.backup(d)"' && ssh ... 'sudo docker cp patrimony:/tmp/app.db /opt/patrimony/backups/'
    PATRIMONY_BACKUP_PASS=... python scripts/backup.py encrypt app.db app-<date>.pat.b64
    # (copier app-<date>.pat.b64 HORS de la machine : autre disque/autre site)
    # restauration :
    PATRIMONY_BACKUP_PASS=... python scripts/backup.py decrypt app-<date>.pat.b64 app.db
    ssh cpt-claude@<hote> 'sudo docker cp app.db patrimony:/tmp/ && \
        sudo docker exec patrimony sh -c "mv data/app.db data/app.db.avant-restore && \
        mv /tmp/app.db data/app.db" && sudo docker restart patrimony'
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from backup_crypto import decrypt_bytes, encrypt_bytes  # noqa: E402


def _passphrase() -> str:
    pw = os.environ.get("PATRIMONY_BACKUP_PASS")
    if pw:
        return pw
    if not sys.stdin.isatty():
        raise SystemExit("PATRIMONY_BACKUP_PASS requise en mode non interactif")
    return getpass.getpass("Mot de passe de sauvegarde : ")


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in ("encrypt", "decrypt"):
        print(__doc__)
        return 2
    cmd, src, dst = sys.argv[1:]
    pw = _passphrase()
    if cmd == "encrypt":
        if len(pw) < 8:
            raise SystemExit("Mot de passe trop court (8 caractères minimum)")
        _write(dst, encrypt_bytes(_read(src), pw).encode("ascii"))
    else:
        _write(dst, decrypt_bytes(_read(src).decode("ascii"), pw))
    print(f"{cmd} terminé : {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
