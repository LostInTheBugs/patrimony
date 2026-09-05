"""Disclaimer de page de login (v2026.09.022) : env DISCLAIMER exposé
publiquement via /api/version, sans valeur si non configuré.
(Env de test posé par tests/test_app.py — ordre alphabétique garanti.)"""
import os

import pytest
from fastapi.testclient import TestClient

import src.app as app


@pytest.fixture(scope="module")
def client():
    return TestClient(app.app)


def test_version_disclaimer_passthrough(client):
    os.environ["DISCLAIMER"] = "Démo publique — données fictives."
    try:
        d = client.get("/api/version").json()
        assert d["version"].startswith("2026.09.")
        assert d["disclaimer"] == "Démo publique — données fictives."
    finally:
        del os.environ["DISCLAIMER"]


def test_version_disclaimer_empty_by_default(client):
    os.environ.pop("DISCLAIMER", None)
    d = client.get("/api/version").json()
    assert d["disclaimer"] is None
    assert app.VERSION.startswith("2026.09.")
