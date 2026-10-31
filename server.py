#!/usr/bin/env python3
"""
CraftStats - lokale Statistik-Seite fuer einen Minecraft-Server (z. B. G-Portal).

Holt die Spieler-Statistiken per FTP (oder aus einem lokalen Ordner),
wertet sie aus und zeigt sie als Ranglisten im Browser an.
Benoetigt nur Python 3.8+ - keine zusaetzlichen Pakete.

Start:  python server.py   ->  http://localhost:8080
"""
import ftplib
import io
import json
import shutil
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
STATS_DIR = CACHE / "stats"
PUBLIC = BASE / "public" if (BASE / "public" / "index.html").exists() else BASE  # GitHub-Version: index.html liegt direkt im Hauptordner
META_FILE = CACHE / "meta.json"
NAMES_FILE = CACHE / "names.json"

# Moegliche Orte der Stats-Dateien relativ zum Weltordner.
# 26.1+ : world/players/stats   |   bis 1.21.11 : world/stats
STATS_CANDIDATES = [("players", "stats"), ("stats",)]
NAME_FILES = ["usercache.json", "whitelist.json", "ops.json"]


def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "server_name": "BBCerver",
    "source": "ftp",
    "ftp": {"host": "", "port": 21, "user": "", "password": "", "tls": False, "base_path": ""},
    "local_path": "",
    "world_name": "",
    "minecraft": {"host": "", "port": 25565},
    "sync_interval_minutes": 10,
    "web_port": 8080,
    "hidden_players": [],
}


def config_from_env():
    """Konfiguration aus Umgebungsvariablen (fuer GitHub Actions)."""
    import os
    e = lambda k, d="": (os.environ.get(k) or d).strip()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["server_name"] = e("SERVER_NAME", cfg["server_name"])
    cfg["ftp"].update(host=e("FTP_HOST"), port=int(e("FTP_PORT", "21") or 21), user=e("FTP_USER"),
                      password=os.environ.get("FTP_PASSWORD", ""), tls=e("FTP_TLS").lower() in ("1", "true", "ja"),
                      base_path=e("FTP_BASE_PATH"))
    cfg["world_name"] = e("WORLD_NAME")
    cfg["minecraft"].update(host=e("MC_HOST"), port=int(e("MC_PORT", "25565") or 25565))
    cfg["hidden_players"] = [x.strip() for x in e("HIDDEN_PLAYERS").split(",") if x.strip()]
    return cfg


def load_config():
    import os
    if os.environ.get("CRAFTSTATS_ENV"):
        return config_from_env()
    path = BASE / "config.json"
    if not path.exists():
        log("config.json nicht gefunden. Kopiere config.example.json nach config.json und trage deine Daten ein.")
        sys.exit(1)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log(f"config.json ist kein gueltiges JSON: {e}")
        sys.exit(1)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


CFG = load_config()


def rel_join(*parts):
    """Pfadteile mit / verbinden. Fuehrender / bleibt nur erhalten, wenn base_path ihn hat."""
    lead = "/" if str(parts[0]).startswith("/") else ""
    return lead + "/".join(str(p).strip("/") for p in parts if p and str(p).strip("/"))


def parse_level_name(props_text):
    for line in props_text.splitlines():
        line = line.strip()
        if line.startswith("level-name="):
            return line.split("=", 1)[1].strip() or "world"
    return "world"


# --------------------------------------------------------------------------- #
# Datenquellen
# --------------------------------------------------------------------------- #
class FtpSource:
    def __init__(self, c, force_tls=False):
        self.c = c
        self.tls = bool(c.get("tls")) or force_tls
        self.ftp = None
        self.errors = []
        self.pwd = "?"

    def _err(self, op, path, e):
        msg = f"{op} '{path or '/'}': {type(e).__name__}: {e}".strip()
        if msg not in self.errors and len(self.errors) < 8:
            self.errors.append(msg)

    def __enter__(self):
        c = self.c
        if not c.get("host") or not c.get("user"):
            raise RuntimeError("FTP-Zugangsdaten fehlen in config.json (ftp.host / ftp.user / ftp.password).")
        host = str(c["host"]).strip()
        for pre in ("ftp://", "ftps://", "sftp://"):
            if host.lower().startswith(pre):
                host = host[len(pre):]
        host = host.rstrip("/").split("/")[0]
        port = int(c.get("port") or 21)
        if ":" in host:  # "ip:port" in host eingetragen
            host, _, hp = host.partition(":")
            port = int(hp) if hp.isdigit() else port
        cls = ftplib.FTP_TLS if self.tls else ftplib.FTP
        self.ftp = cls(timeout=30)
        self.ftp.encoding = "utf-8"
        self.ftp.connect(host, port)
        self.ftp.login(str(c["user"]).strip(), c.get("password", ""))
        if self.tls:
            self.ftp.prot_p()
        self.ftp.set_pasv(True)
        try:
            self.pwd = self.ftp.pwd()
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass

    def path(self, *parts):
        return rel_join(self.c.get("base_path", ""), *parts)

    def read(self, rel_parts):
        p = self.path(*rel_parts)
        buf = io.BytesIO()
        try:
            self.ftp.retrbinary("RETR " + p, buf.write)
        except ftplib.error_perm as e:
            if not str(e).startswith("550"):
                self._err("RETR", p, e)
            return None
        except Exception as e:
            self._err("RETR", p, e)
            return None
        return buf.getvalue()

    def _list_lines(self, p):
        lines = []
        self.ftp.retrlines("LIST" + (" " + p if p else ""), lines.append)
        return lines

    def list_entries(self, rel_parts):
        """Gibt [(name, ist_ordner, groesse, aenderung)] zurueck oder None, wenn der Ordner fehlt."""
        p = self.path(*rel_parts)
        # 1) MLSD
        try:
            out = []
            for n, f in self.ftp.mlsd(p, facts=["type", "size", "modify"]):
                t = f.get("type", "file").lower()
                if t in ("cdir", "pdir") or n in (".", ".."):
                    continue
                out.append((n, t == "dir", f.get("size"), f.get("modify")))
            return out
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                return None
            self._err("MLSD", p, e)
        except Exception as e:
            self._err("MLSD", p, e)
        # 2) LIST (Unix- oder Windows-Format)
        try:
            out = []
            for line in self._list_lines(p):
                parts = line.split(None, 8)
                if len(parts) >= 9 and parts[0][:1] in "d-l":
                    name = parts[8]
                    if name in (".", ".."):
                        continue
                    out.append((name, parts[0].startswith("d"), parts[4], None))
                elif len(parts) >= 4 and parts[2].upper() == "<DIR>":
                    out.append((line.split(None, 3)[3], True, None, None))
                elif len(parts) >= 4 and parts[2].isdigit():
                    out.append((line.split(None, 3)[3], False, parts[2], None))
            return out
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                return None
            self._err("LIST", p, e)
        except Exception as e:
            self._err("LIST", p, e)
        return []

    def list_dirs(self, rel_parts):
        entries = self.list_entries(rel_parts) or []
        return sorted(n for n, is_dir, _, _ in entries if is_dir)

    def list_json(self, rel_parts):
        """Gibt {dateiname: (groesse, aenderungszeit)} zurueck oder None, wenn Ordner fehlt."""
        entries = self.list_entries(rel_parts)
        if entries is None:
            return None
        p = self.path(*rel_parts)
        result = {}
        for n, is_dir, size, mod in entries:
            if is_dir or not n.endswith(".json"):
                continue
            if mod is None:
                try:
                    mod = self.ftp.sendcmd("MDTM " + p + "/" + n).split()[-1]
                except Exception:
                    pass
            result[n] = (size, mod)
        return result

    def root_overview(self):
        entries = self.list_entries([]) or []
        return ", ".join((n + "/" if d else n) for n, d, _, _ in entries[:25]) or "(leer)"


class LocalSource:
    def __init__(self, path):
        self.root = Path(path)

    def __enter__(self):
        if not self.root.is_dir():
            raise RuntimeError(f"Lokaler Ordner nicht gefunden: {self.root}")
        return self

    def __exit__(self, *exc):
        pass

    def read(self, rel_parts):
        p = self.root.joinpath(*rel_parts)
        return p.read_bytes() if p.is_file() else None

    def list_dirs(self, rel_parts):
        p = self.root.joinpath(*rel_parts)
        return sorted(d.name for d in p.iterdir() if d.is_dir()) if p.is_dir() else []

    def list_json(self, rel_parts):
        p = self.root.joinpath(*rel_parts)
        if not p.is_dir():
            return None
        return {
            f.name: (str(f.stat().st_size), time.strftime("%Y%m%d%H%M%S", time.gmtime(f.stat().st_mtime)))
            for f in p.glob("*.json")
        }


def ftp_time_to_epoch(mod):
    if not mod:
        return None
    try:
        import calendar
        return calendar.timegm(time.strptime(mod[:14], "%Y%m%d%H%M%S"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Synchronisation
# --------------------------------------------------------------------------- #
STATE = {"last_sync": None, "sync_error": None, "syncing": False, "world": None, "stats_path": None}
STATE_LOCK = threading.Lock()
DATA_CACHE = {"payload": None}


def read_json_file(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


LOCATE_CACHE = {}


class StatsNotFound(RuntimeError):
    pass


def _try_world(src, prefix, world):
    for cand in STATS_CANDIDATES:
        listing = src.list_json([*prefix, world, *cand])
        if listing:
            return cand, listing
    return None, None


def locate_stats(src):
    """Sucht den Stats-Ordner: erst im FTP-Hauptordner, dann eine Ebene tiefer.
    Gibt (unterordner-teile, weltname, stats-teile, dateiliste) zurueck."""
    forced_world = CFG.get("world_name") or ""
    # Zuletzt gefundenen Ort zuerst probieren (spart Zeit)
    if LOCATE_CACHE:
        c = LOCATE_CACHE
        found, listing = _try_world(src, c["prefix"], c["world"])
        if listing:
            return c["prefix"], c["world"], found, listing

    root_dirs = src.list_dirs([])
    prefixes = [[]] + [[d] for d in root_dirs]
    tried = []
    for prefix in prefixes:
        props = src.read([*prefix, "server.properties"])
        worlds = []
        if forced_world:
            worlds.append(forced_world)
        if props:
            worlds.append(parse_level_name(props.decode("utf-8", "replace")))
        if not prefix:
            worlds.append("world")
        if props or not prefix:
            # Weltname unbekannt/abweichend: alle Unterordner pruefen
            sub = src.list_dirs(prefix) if prefix else root_dirs
            worlds += [d for d in sub if d not in worlds][:40]
        for world in dict.fromkeys(worlds):
            found, listing = _try_world(src, prefix, world)
            tried.append("/".join([*prefix, world]))
            if listing:
                LOCATE_CACHE.update(prefix=prefix, world=world)
                if prefix or world != "world":
                    log(f"Weltordner gefunden: {'/'.join([*prefix, world])}")
                return prefix, world, found, listing

    shown = ", ".join(root_dirs[:25]) or "(keine Ordner)"
    raise StatsNotFound(
        "Keine Stats-Dateien gefunden. Ordner im Hauptverzeichnis: " + shown + ". "
        "Gesucht wurde in players/stats bzw. stats unter: " + ", ".join(tried[:12]) +
        (" ..." if len(tried) > 12 else "") +
        ". Entweder hat noch niemand gespielt, oder die Welt liegt tiefer - dann ftp.base_path in config.json setzen."
    )


def do_sync():
    with STATE_LOCK:
        if STATE["syncing"]:
            return
        STATE["syncing"] = True
    started = time.time()
    if CFG["source"] == "ftp" and not CFG["ftp"].get("tls") and not STATE.get("tls_tried"):
        # Manche Hoster erlauben Dateiuebertragung nur verschluesselt: einmal testen
        try:
            with FtpSource(CFG["ftp"]) as probe:
                ok = probe.read(["server.properties"]) is not None or probe.list_dirs([])
                errs = list(probe.errors)
            if not ok and errs:
                with FtpSource(CFG["ftp"], force_tls=True) as probe2:
                    if probe2.read(["server.properties"]) is not None or probe2.list_dirs([]):
                        CFG["ftp"]["tls"] = True
                        log("Hinweis: Der FTP-Server braucht eine verschluesselte Verbindung - TLS wird jetzt benutzt. "
                            'Du kannst in config.json "tls": true eintragen.')
        except Exception:
            pass
        STATE["tls_tried"] = True
    try:
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        meta = read_json_file(META_FILE, {})
        if CFG["source"] == "ftp":
            src = FtpSource(CFG["ftp"])
        else:
            src = LocalSource(CFG["local_path"])
        with src:
            try:
                prefix, world, found, listing = locate_stats(src)
            except StatsNotFound as nf:
                if isinstance(src, FtpSource):
                    overview = src.root_overview()
                    details = f" Inhalt des FTP-Hauptordners ({src.pwd}): {overview}."
                    if src.errors:
                        details += " FTP-Meldungen: " + " | ".join(src.errors)
                    raise RuntimeError(str(nf) + details)
                raise
            wparts = [*prefix, world]

            changed = 0
            for name, (size, mod) in listing.items():
                local = STATS_DIR / name
                old = meta.get(name)
                if local.exists() and old and old.get("size") == size and old.get("mod") == mod and mod:
                    continue
                raw = src.read([*wparts, *found, name])
                if raw is None:
                    continue
                local.write_bytes(raw)
                meta[name] = {"size": size, "mod": mod}
                changed += 1

            # Dateien entfernen, die es auf dem Server nicht mehr gibt
            for f in STATS_DIR.glob("*.json"):
                if f.name not in listing:
                    f.unlink()
                    meta.pop(f.name, None)

            for nf in NAME_FILES:
                raw = src.read([*prefix, nf])
                if raw:
                    (CACHE / nf).write_bytes(raw)

        META_FILE.write_text(json.dumps(meta), encoding="utf-8")
        with STATE_LOCK:
            STATE.update(last_sync=time.time(), sync_error=None, world=world, stats_path="/".join([*wparts, *found]))
        DATA_CACHE["payload"] = None
        log(f"Sync ok: {len(listing)} Spieler, {changed} aktualisiert ({time.time() - started:.1f}s) aus {'/'.join([*wparts, *found])}")
        threading.Thread(target=resolve_missing_names, daemon=True).start()
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        with STATE_LOCK:
            STATE["sync_error"] = msg
        log("Sync fehlgeschlagen:", msg)
        if not isinstance(e, (RuntimeError, ftplib.Error, OSError)):
            traceback.print_exc()
    finally:
        with STATE_LOCK:
            STATE["syncing"] = False


def sync_loop():
    interval = max(1, float(CFG.get("sync_interval_minutes", 10))) * 60
    while True:
        do_sync()
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# Spielernamen
# --------------------------------------------------------------------------- #
NAMES_LOCK = threading.Lock()


def known_names():
    names = read_json_file(NAMES_FILE, {})
    for nf in NAME_FILES:
        for entry in read_json_file(CACHE / nf, []):
            if isinstance(entry, dict) and entry.get("uuid") and entry.get("name"):
                names[entry["uuid"].lower()] = entry["name"]
    return names


def resolve_missing_names():
    """Fragt Namen unbekannter Online-Mode-UUIDs bei Mojang an."""
    with NAMES_LOCK:
        names = known_names()
        stored = read_json_file(NAMES_FILE, {})
        todo = [f.stem.lower() for f in STATS_DIR.glob("*.json") if f.stem.lower() not in names]
        todo = [u for u in todo if len(u) == 36 and u[14] == "4"]  # nur echte Mojang-UUIDs (Version 4)
        updated = False
        for uuid in todo:
            try:
                url = "https://sessionserver.mojang.com/session/minecraft/profile/" + uuid.replace("-", "")
                with urllib.request.urlopen(url, timeout=8) as r:
                    if r.status == 200:
                        stored[uuid] = json.loads(r.read())["name"]
                        updated = True
                time.sleep(0.3)
            except Exception:
                pass
        if updated:
            NAMES_FILE.write_text(json.dumps(stored, indent=1), encoding="utf-8")
            DATA_CACHE["payload"] = None
            log(f"{sum(1 for u in todo if u in stored)} Spielernamen bei Mojang nachgeschlagen")


# --------------------------------------------------------------------------- #
# Auswertung
# --------------------------------------------------------------------------- #
def strip_ns(key):
    return key.split(":", 1)[-1]


def top(d, n=10):
    items = sorted(((strip_ns(k), v) for k, v in d.items() if isinstance(v, (int, float)) and v > 0),
                   key=lambda x: -x[1])
    return items[:n]


def build_payload():
    names = known_names()
    hidden = {h.lower() for h in CFG.get("hidden_players", [])}
    meta = read_json_file(META_FILE, {})
    players = []
    for f in STATS_DIR.glob("*.json"):
        uuid = f.stem.lower()
        name = names.get(uuid)
        if uuid in hidden or (name and name.lower() in hidden):
            continue
        data = read_json_file(f, {})
        s = data.get("stats", {})
        custom = s.get("minecraft:custom", {})
        mined = s.get("minecraft:mined", {})
        crafted = s.get("minecraft:crafted", {})
        used = s.get("minecraft:used", {})
        killed = s.get("minecraft:killed", {})
        killed_by = s.get("minecraft:killed_by", {})
        c = lambda k: custom.get("minecraft:" + k, 0)

        dist_cm = sum(v for k, v in custom.items()
                      if k.endswith("_one_cm") and k != "minecraft:fall_one_cm")
        movement = {strip_ns(k).replace("_one_cm", ""): round(v / 100000, 2)
                    for k, v in custom.items() if k.endswith("_one_cm") and v > 0}

        metrics = {
            "playtime": round(c("play_time") / 20 / 3600, 2),
            "deaths": c("deaths"),
            "mob_kills": c("mob_kills"),
            "player_kills": c("player_kills"),
            "distance": round(dist_cm / 100000, 2),
            "blocks_mined": sum(mined.values()),
            "diamonds": mined.get("minecraft:diamond_ore", 0) + mined.get("minecraft:deepslate_diamond_ore", 0),
            "ancient_debris": mined.get("minecraft:ancient_debris", 0),
            "crafted": sum(crafted.values()),
            "jumps": c("jump"),
            "elytra": round(c("aviate_one_cm") / 100000, 2),
            "fish": c("fish_caught"),
            "trades": c("traded_with_villager"),
            "damage_dealt": round(c("damage_dealt") / 10),
            "damage_taken": round(c("damage_taken") / 10),
            "items_used": sum(used.values()),
        }
        last_mod = ftp_time_to_epoch((meta.get(f.name) or {}).get("mod")) or int(f.stat().st_mtime)
        players.append({
            "uuid": uuid,
            "name": name or uuid[:8],
            "name_known": bool(name),
            "last_saved": last_mod,
            "metrics": metrics,
            "details": {
                "mined": top(mined),
                "killed": top(killed),
                "killed_by": top(killed_by, 5),
                "crafted": top(crafted, 5),
                "movement": sorted(movement.items(), key=lambda x: -x[1]),
            },
        })
    with STATE_LOCK:
        state = dict(STATE)
    return {
        "server_name": CFG.get("server_name"),
        "last_sync": state["last_sync"],
        "sync_error": state["sync_error"],
        "syncing": state["syncing"],
        "stats_path": state["stats_path"],
        "sync_interval_minutes": CFG.get("sync_interval_minutes"),
        "players": players,
    }


# --------------------------------------------------------------------------- #
# Server-Status (Minecraft Server List Ping)
# --------------------------------------------------------------------------- #
def _varint(n):
    n &= 0xFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("Verbindung geschlossen")
        buf += chunk
    return buf


def _read_varint(sock):
    n = 0
    for i in range(5):
        b = _recv_exact(sock, 1)[0]
        n |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return n
    raise IOError("VarInt zu lang")


def _flatten(desc):
    if isinstance(desc, str):
        return desc
    if isinstance(desc, list):
        return "".join(_flatten(x) for x in desc)
    if isinstance(desc, dict):
        return desc.get("text", "") + "".join(_flatten(x) for x in desc.get("extra", []))
    return ""


def ping_server(host, port):
    start = time.time()
    with socket.create_connection((host, port), timeout=5) as s:
        s.settimeout(5)
        h = host.encode()
        hs = b"\x00" + _varint(-1) + _varint(len(h)) + h + struct.pack(">H", port) + _varint(1)
        s.sendall(_varint(len(hs)) + hs + b"\x01\x00")
        _read_varint(s)  # Paketlaenge
        _read_varint(s)  # Paket-ID
        data = json.loads(_recv_exact(s, _read_varint(s)).decode("utf-8"))
    pl = data.get("players", {})
    import re
    return {
        "online": True,
        "latency_ms": round((time.time() - start) * 1000),
        "version": data.get("version", {}).get("name"),
        "motd": re.sub("§.", "", _flatten(data.get("description", ""))).strip(),
        "players_online": pl.get("online", 0),
        "players_max": pl.get("max", 0),
        "sample": [{"name": p.get("name"), "uuid": p.get("id")} for p in pl.get("sample", []) or []],
    }


STATUS_CACHE = {"t": 0, "data": None}


def get_status():
    mc = CFG.get("minecraft", {})
    if not mc.get("host"):
        return {"configured": False}
    if time.time() - STATUS_CACHE["t"] < 30 and STATUS_CACHE["data"]:
        return STATUS_CACHE["data"]
    try:
        data = ping_server(mc["host"], int(mc.get("port") or 25565))
    except Exception as e:
        data = {"online": False, "error": str(e)}
    data["configured"] = True
    STATUS_CACHE.update(t=time.time(), data=data)
    return data


# --------------------------------------------------------------------------- #
# Webserver
# --------------------------------------------------------------------------- #
LAST_MANUAL = {"t": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/data":
            if DATA_CACHE["payload"] is None:
                DATA_CACHE["payload"] = build_payload()
            payload = dict(DATA_CACHE["payload"])
            with STATE_LOCK:
                payload["syncing"] = STATE["syncing"]
                payload["sync_error"] = STATE["sync_error"]
            return self.send_json(payload)
        if path == "/api/status":
            return self.send_json(get_status())
        if path in ("/", "/index.html"):
            body = (PUBLIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/sync":
            if time.time() - LAST_MANUAL["t"] < 20:
                return self.send_json({"ok": False, "message": "Bitte kurz warten"}, 429)
            LAST_MANUAL["t"] = time.time()
            threading.Thread(target=do_sync, daemon=True).start()
            return self.send_json({"ok": True})
        self.send_error(404)


def main():
    CACHE.mkdir(exist_ok=True)
    threading.Thread(target=sync_loop, daemon=True).start()
    port = int(CFG.get("web_port", 8080))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"CraftStats laeuft auf http://localhost:{port}  (Beenden mit Strg+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Beendet.")


if __name__ == "__main__":
    main()
