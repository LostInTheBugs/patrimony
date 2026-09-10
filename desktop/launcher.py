"""Patrimony Desktop — lance le serveur local puis ouvre la fenêtre native.

Double-clic sur Patrimony.exe : le backend (uvicorn) démarre sur un port
local libre (127.0.0.1), la fenêtre s'ouvre sur l'application. Les données
vivent dans « data/ », à côté de l'exécutable — sauvegarder = copier ce
dossier, rien ne sort de la machine.

Variables d'environnement :
  PATRIMONY_NO_WINDOW=1  → mode sans fenêtre (serveur seul ; tests, CI)
  PATRIMONY_PORT=<port>  → port fixe (défaut : port libre automatique)
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path


def base_dir() -> Path:
    """Dossier de travail : à côté de l'exécutable (bundle) ou racine du dépôt (dev)."""
    if getattr(sys, "frozen", False):  # exécutable PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_server(port: int, tries: int = 200) -> bool:
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> None:
    base = base_dir()
    os.environ.setdefault("DATA_DIR", str(base / "data"))
    os.environ.setdefault("COOKIE_SECURE", "0")
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(base))  # dev : rendre « src » importable

    import uvicorn
    from src.app import app

    port = int(os.environ.get("PATRIMONY_PORT") or 0) or free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    wait_server(port)

    if os.environ.get("PATRIMONY_NO_WINDOW") == "1":
        print(url, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    try:
        import webview  # fenêtre native (WebView2 sous Windows)

        webview.create_window(
            "Patrimony", url, width=1240, height=840, min_size=(920, 620)
        )
        webview.start()  # bloque jusqu'à la fermeture de la fenêtre
        return
    except Exception:  # fenêtre indisponible → navigateur par défaut
        import webbrowser

        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
