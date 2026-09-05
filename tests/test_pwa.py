"""Tests PWA (v2026.09.017) : manifest, service worker, icônes."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="patrimony-pwa-")
os.environ["DATA_DIR"] = _tmp
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-test-2026"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SEED_DEMO"] = "0"

from fastapi.testclient import TestClient

import src.app as app


def test_manifest_pwa():
    r = TestClient(app.app).get("/manifest.webmanifest")
    assert r.status_code == 200
    m = r.json()
    assert m["name"].startswith("Patrimony")
    assert m["display"] == "standalone"
    assert m["start_url"] == "/"
    assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_service_worker_versioned_and_api_never_cached():
    r = TestClient(app.app).get("/sw.js")
    assert r.status_code == 200
    body = r.text
    assert "patrimony-" + app.VERSION in body  # nom de cache versionné
    assert "skipWaiting" in body
    assert "/api/" in body and "return;" in body  # l'API n'est jamais mise en cache
    # le manifest référence des icônes qui existent
    for icon in TestClient(app.app).get("/manifest.webmanifest").json()["icons"]:
        assert TestClient(app.app).get(icon["src"]).status_code == 200
    assert TestClient(app.app).get("/icons/apple-touch-icon.png").status_code == 200


def test_pwa_head_links():
    html = TestClient(app.app).get("/index.html").text
    assert 'rel="manifest"' in html
    assert "serviceWorker.register('/sw.js')" in html
    assert "apple-touch-icon" in html
