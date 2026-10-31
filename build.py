#!/usr/bin/env python3
"""
Baut die Online-Version der Stats-Seite (fuer GitHub Pages).

Holt die Stats per FTP, verschluesselt sie mit dem Seiten-Passwort und legt
alles in den Ordner _site/. Die Zugangsdaten kommen aus Umgebungsvariablen
(bei GitHub: Settings -> Secrets and variables -> Actions).

WICHTIG: Dieses Skript gibt keine Spielerdaten und keine Passwoerter aus,
weil die Logs bei oeffentlichen GitHub-Projekten fuer alle sichtbar sind.
"""
import base64
import gzip
import hashlib
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

os.environ["CRAFTSTATS_ENV"] = "1"

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

import server  # noqa: E402

ITERATIONS = 300_000
OUT = Path(__file__).resolve().parent / "_site"


def fail(msg):
    print("FEHLER:", msg, flush=True)
    sys.exit(1)


def main():
    password = os.environ.get("SITE_PASSWORD", "")
    if len(password) < 6:
        fail("Das Secret SITE_PASSWORD fehlt oder ist kuerzer als 6 Zeichen. "
             "Ohne Passwort wird aus Sicherheitsgruenden nichts veroeffentlicht.")
    if not server.CFG["ftp"]["host"] or not server.CFG["ftp"]["user"]:
        fail("Die Secrets FTP_HOST und/oder FTP_USER fehlen.")

    server.CACHE.mkdir(exist_ok=True)
    server.do_sync()
    if server.STATE["sync_error"]:
        fail("Stats konnten nicht geladen werden: " + server.STATE["sync_error"])
    server.resolve_missing_names()

    payload = server.build_payload()
    payload["status"] = server.get_status()
    payload["status_checked"] = time.time()
    payload["mode"] = "online"
    payload.pop("stats_path", None)

    plain = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, 32)
    cipher = AESGCM(key).encrypt(iv, plain, None)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    b64 = lambda b: base64.b64encode(b).decode()
    (OUT / "data.enc.json").write_text(json.dumps({
        "v": 1, "iter": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "data": b64(cipher),
    }), encoding="utf-8")

    html = (server.PUBLIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", "<script>window.CRAFTSTATS_STATIC=true</script>\n</head>", 1)
    (OUT / "index.html").write_text(html, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    (OUT / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    print(f"Fertig: {len(payload['players'])} Spieler, Daten verschluesselt ({len(cipher) // 1024} KB).", flush=True)


if __name__ == "__main__":
    main()
