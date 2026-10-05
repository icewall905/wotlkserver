"""AzerothCore management dashboard.

Small Flask app that talks to Docker (container control and logs), MySQL
(players, accounts) and the worldserver SOAP interface (GM commands).
"""
import datetime
import functools
import gzip
import hmac
import json
import os
import re
import shutil
import struct
from concurrent.futures import ThreadPoolExecutor
import subprocess
import threading
import time
import xml.sax.saxutils as xml_escape

import docker
import pymysql
import requests
from flask import Flask, Response, jsonify, render_template, request

from agents import AgentRunner

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "ac-database")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASS = os.environ.get("DB_PASS", "password")
SOAP_URL = os.environ.get("SOAP_URL", "http://ac-worldserver:7878/")
SOAP_USER = os.environ.get("SOAP_USER", "")
SOAP_PASS = os.environ.get("SOAP_PASS", "")
ADMIN_USER = os.environ.get("MANAGER_USER", "admin")
ADMIN_PASS = os.environ.get("MANAGER_PASSWORD", "")
LLM_BASE = os.environ.get("LLM_BASE", "http://localhost:8000")  # fallback only; OllamaChat.Url wins
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
OLLAMA_CONF = os.environ.get("OLLAMA_CONF", "/etc-ac/modules/mod_ollama_chat.conf")
PLAYERBOTS_CONF = os.environ.get("PLAYERBOTS_CONF", "/etc-ac/modules/playerbots.conf")
AREA_DBC = os.environ.get("AREA_DBC", "/dbc/AreaTable.dbc")
MAP_DBC = os.environ.get("MAP_DBC", "/dbc/Map.dbc")

CONTAINERS = ["ac-worldserver", "ac-authserver", "ac-database"]
OVERVIEW_CONTAINERS = CONTAINERS + ["ac-manager"]
CONTROLLABLE = {"ac-worldserver", "ac-authserver"}

CLASSES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest", 6: "Death Knight",
           7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
RACES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren", 7: "Gnome",
         8: "Troll", 10: "Blood Elf", 11: "Draenei"}
MAPS = {0: "Eastern Kingdoms", 1: "Kalimdor", 530: "Outland", 571: "Northrend", 609: "Ebon Hold"}

QUALITIES = ["Poor", "Common", "Uncommon", "Rare", "Epic", "Legendary", "Artifact", "Heirloom"]
SLOTS = {1: "Head", 2: "Neck", 3: "Shoulder", 4: "Shirt", 5: "Chest", 6: "Waist", 7: "Legs", 8: "Feet",
         9: "Wrist", 10: "Hands", 11: "Finger", 12: "Trinket", 13: "One-Hand", 14: "Shield", 15: "Ranged",
         16: "Back", 17: "Two-Hand", 18: "Bag", 19: "Tabard", 20: "Chest", 21: "Main Hand", 22: "Off Hand",
         23: "Held In Off-hand", 24: "Ammo", 25: "Thrown", 26: "Ranged", 28: "Relic"}

# Armor / weapon subclasses each class can use (at max level).
CLASS_ARMOR = {1: {1, 2, 3, 4, 6}, 2: {1, 2, 3, 4, 6, 7}, 3: {1, 2, 3}, 4: {1, 2}, 5: {1}, 6: {1, 2, 3, 4, 10},
               7: {1, 2, 3, 6, 9}, 8: {1}, 9: {1}, 11: {1, 2, 8}}
CLASS_WEAPONS = {1: {0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13, 15, 16, 18}, 2: {0, 1, 4, 5, 6, 7, 8},
                 3: {0, 1, 2, 3, 6, 7, 8, 10, 13, 15, 16, 18}, 4: {0, 2, 3, 4, 7, 13, 15, 16, 18},
                 5: {4, 10, 15, 19}, 6: {0, 1, 4, 5, 6, 7, 8}, 7: {0, 1, 4, 5, 10, 13, 15},
                 8: {7, 10, 15, 19}, 9: {7, 10, 15, 19}, 11: {4, 5, 6, 10, 13, 15}}
ARMOR_TYPES = {0: "Misc", 1: "Cloth", 2: "Leather", 3: "Mail", 4: "Plate", 6: "Shield", 7: "Libram", 8: "Idol",
               9: "Totem", 10: "Sigil"}
WEAPON_TYPES = {0: "Axe", 1: "Two-Hand Axe", 2: "Bow", 3: "Gun", 4: "Mace", 5: "Two-Hand Mace", 6: "Polearm",
                7: "Sword", 8: "Two-Hand Sword", 10: "Staff", 13: "Fist Weapon", 14: "Misc", 15: "Dagger",
                16: "Thrown", 18: "Crossbow", 19: "Wand", 20: "Fishing Pole"}
STATS = {3: "Agi", 4: "Str", 5: "Int", 6: "Spi", 7: "Sta", 12: "Def", 13: "Dodge", 14: "Parry", 15: "Block",
         31: "Hit", 32: "Crit", 35: "Resil", 36: "Haste", 37: "Expertise", 38: "AP", 43: "MP5", 44: "ArP",
         45: "SP", 46: "HP5", 47: "SpellPen", 48: "BlockValue"}
JUNK_NAME = ("name NOT LIKE '%%DEPRECATED%%' AND name NOT LIKE 'Test %%' AND name NOT LIKE '%%(test)%%' "
             "AND name NOT LIKE 'Monster -%%' AND name NOT LIKE '%%OLD%%' AND name NOT LIKE 'zz%%' "
             "AND name NOT LIKE '[PH]%%' AND name NOT LIKE 'QA %%' AND name NOT LIKE '%% Test' "
             "AND name NOT LIKE '%%Test %%Item%%' AND name NOT LIKE 'Unused%%' AND name NOT LIKE '%%(old)%%' "
             "AND name NOT LIKE 'NPC %%' AND name NOT LIKE 'Dev %%'")
ITEM_SORTS = {"ilvl": "ItemLevel DESC, Quality DESC", "req": "RequiredLevel DESC, ItemLevel DESC",
              "quality": "Quality DESC, ItemLevel DESC", "name": "name ASC"}

USERNAME_RE = re.compile(r"^[A-Za-z0-9]{3,16}$")
CHARNAME_RE = re.compile(r"^[^\W\d_]{2,12}$")
docker_client = docker.from_env()
backup_lock = threading.Lock()


# --------------------------------------------------------------------------- auth

def requires_auth(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not ADMIN_PASS or not auth or auth.username != ADMIN_USER or \
                not hmac.compare_digest(auth.password or "", ADMIN_PASS):
            return Response("Authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="AzerothCore Manager"'})
        return view(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- helpers

def query(sql, args=None):
    conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, connect_timeout=5,
                           cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def load_dbc_names(path, name_field):
    """id -> enUS name from a client DBC file (the DB copies are empty)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        _, count, fields, rec_size, _ = struct.unpack("<4s4I", data[:20])
        strings = data[20 + count * rec_size:]
        names = {}
        for i in range(count):
            rec = struct.unpack_from(f"<{fields}I", data, 20 + i * rec_size)
            end = strings.index(b"\0", rec[name_field])
            names[rec[0]] = strings[rec[name_field]:end].decode("utf-8", "replace")
        return names
    except (OSError, struct.error, ValueError):
        return {}


ZONES = load_dbc_names(AREA_DBC, 11)
MAP_NAMES = load_dbc_names(MAP_DBC, 5)


def load_zone_bounds():
    """Zone id -> (left, right, top, bottom) world coordinates of its in-game map (WorldMapArea.dbc)."""
    try:
        with open("/dbc/WorldMapArea.dbc", "rb") as f:
            data = f.read()
        _, count, _, rec_size, _ = struct.unpack("<4s4I", data[:20])
        out = {}
        for i in range(count):
            _id, _map, area, _name, left, right, top, bottom, display_map, _floor, _parent = \
                struct.unpack_from("<3I I 4f 3i", data, 20 + i * rec_size)
            # Draenei/blood elf zones carry a display map (shown on Kalimdor/EK); keep them too,
            # but prefer the plain entry when a zone has several.
            if area and (display_map == -1 or area not in out):
                out[area] = (left, right, top, bottom)
        return out
    except (OSError, struct.error):
        return {}


def load_item_icons():
    """Item display id -> icon name (ItemDisplayInfo.dbc)."""
    try:
        with open("/dbc/ItemDisplayInfo.dbc", "rb") as f:
            data = f.read()
        _, count, fields, rec_size, _ = struct.unpack("<4s4I", data[:20])
        strings = data[20 + count * rec_size:]
        out = {}
        for i in range(count):
            rec = struct.unpack_from(f"<{fields}I", data, 20 + i * rec_size)
            off = rec[5]
            if off:
                out[rec[0]] = strings[off:strings.index(b"\0", off)].decode("ascii", "replace").lower()
        return out
    except (OSError, struct.error, ValueError):
        return {}


ZONE_BOUNDS = load_zone_bounds()
ITEM_ICONS = load_item_icons()
ICON_URL = "https://wow.zamimg.com/images/wow/icons/{size}/{name}.jpg"
CLASS_COLORS = {1: "#C79C6E", 2: "#F58CBA", 3: "#ABD473", 4: "#FFF569", 5: "#FFFFFF", 6: "#C41F3B",
                7: "#0070DE", 8: "#69CCF0", 9: "#9482C9", 11: "#FF7D0A"}
CLASS_ICON = {1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest", 6: "deathknight", 7: "shaman",
              8: "mage", 9: "warlock", 11: "druid"}
RACE_ICON = {1: "human", 2: "orc", 3: "dwarf", 4: "nightelf", 5: "scourge", 6: "tauren", 7: "gnome", 8: "troll",
             10: "bloodelf", 11: "draenei"}


def map_point(zone_id, x, y):
    """Position as a fraction of the zone's map image (WoW: x points north, y points west)."""
    b = ZONE_BOUNDS.get(zone_id)
    if not b:
        return None
    left, right, top, bottom = b
    px, py = (left - y) / (left - right), (top - x) / (top - bottom)
    if not (-0.05 <= px <= 1.05 and -0.05 <= py <= 1.05):
        return None
    return round(px, 4), round(py, 4)


def map_name(map_id):
    return MAP_NAMES.get(map_id) or MAPS.get(map_id, f"Map {map_id}")


def readable(name):
    """'GateOfIronforge' -> 'Gate Of Ironforge'."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name).replace("_", " ")


def zone_name(zone_id):
    return ZONES.get(zone_id, f"Zone {zone_id}")


def live_who():
    """(players, bots) in the world right now from ".dash who", or None if the server is down.

    players maps each real player (someone at a client) to whether autopilot is on; bots are their alt bots."""
    ok, text = soap("dash who")
    if not ok:
        return None
    players, bots = {}, set()
    for line in text.splitlines():
        parts = line.split("\t")
        if parts[0] == "P" and len(parts) >= 9:
            players[parts[1]] = len(parts) > 9 and parts[9] == "1"
            bots.update(b for b in parts[8].split(",") if b)
    return players, bots


def live_online_names():
    """Real players and their bots that are in the world right now, or None if the server is down.

    The characters.online flag is unreliable for alts: a logout clears it for every character on
    the account, so sending one alt home makes the others look offline while they still play."""
    who = live_who()
    return None if who is None else set(who[0]) | who[1]


def char_online(name):
    rows = query("SELECT online FROM acore_characters.characters WHERE name = %s", (name,))
    if not rows:
        return None
    live = live_online_names()
    return name in live if live is not None else bool(rows[0]["online"])


def live_level(name):
    """Current level of an online character; the DB copy lags until the next save."""
    ok, out = soap(f"pinfo {name}")
    m = re.search(r"(?<![A-Za-z])Level: (\d+)", out) if ok else None
    return int(m.group(1)) if m else None


def valid_char(name):
    return bool(CHARNAME_RE.match(name or ""))


def execute(sql, args=None):
    conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, connect_timeout=5, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.rowcount
    finally:
        conn.close()


def soap(command, timeout=15):
    if not SOAP_USER:
        return False, "SOAP credentials are not configured (SOAP_USER / SOAP_PASS)."
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="urn:AC">'
        '<SOAP-ENV:Body><ns1:executeCommand><command>'
        f'{xml_escape.escape(command)}'
        '</command></ns1:executeCommand></SOAP-ENV:Body></SOAP-ENV:Envelope>'
    )
    try:
        r = requests.post(SOAP_URL, data=body.encode(), auth=(SOAP_USER, SOAP_PASS), timeout=timeout,
                          headers={"Content-Type": "text/xml"})
    except requests.RequestException as e:
        return False, f"Worldserver unreachable: {e.__class__.__name__}"
    m = re.search(r"<result>(.*?)</result>", r.text, re.S)
    if m:
        return True, xml_unescape(m.group(1)).strip()
    m = re.search(r"<faultstring>(.*?)</faultstring>", r.text, re.S)
    msg = xml_unescape(m.group(1)).strip() if m else f"HTTP {r.status_code}"
    return False, msg


def xml_unescape(s):
    return xml_escape.unescape(s, {"&quot;": '"', "&apos;": "'", "&#xD;": ""})


def container_info(name):
    try:
        c = docker_client.containers.get(name)
    except docker.errors.NotFound:
        return {"name": name, "status": "missing", "health": None, "started": None}
    state = c.attrs["State"]
    return {
        "name": name,
        "status": state["Status"],
        "health": state.get("Health", {}).get("Status"),
        "started": state.get("StartedAt") if state["Status"] == "running" else None,
        "image": c.image.tags[0] if c.image.tags else c.image.short_id,
    }


def read_conf_value(path, key):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(rf"^{re.escape(key)}\s*=\s*(.*?)\s*$", line)
                if m:
                    return m.group(1).strip('"')
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- pages

@app.get("/")
@requires_auth
def index():
    return render_template("index.html")


@app.get("/api/status")
@requires_auth
def api_status():
    services = [container_info(n) for n in CONTAINERS]
    players = {"real": None, "bots": None}
    try:
        row = query(
            "SELECT SUM(a.username NOT LIKE 'RNDBOT%%' AND a.os <> '') AS real_players, "
            "       SUM(a.username LIKE 'RNDBOT%%' OR a.os = '') AS bots "
            "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
            "WHERE c.online = 1")[0]
        players = {"real": int(row["real_players"] or 0), "bots": int(row["bots"] or 0)}
    except pymysql.MySQLError:
        pass
    return jsonify(services=services, players=players)


@app.get("/api/pulse")
@requires_auth
def api_pulse():
    """Cheap heartbeat for the sidebar: is the world up and how many people are playing."""
    who = live_who()
    return jsonify(world_up=who is not None, players=len(who[0]) if who else 0,
                   autopilot=sum(who[0].values()) if who else 0)


@app.get("/api/llm")
@requires_auth
def api_llm():
    url = read_conf_value(OLLAMA_CONF, "OllamaChat.Url")
    model = read_conf_value(OLLAMA_CONF, "OllamaChat.Model")
    enabled = read_conf_value(OLLAMA_CONF, "OllamaChat.Enable")
    out = {"url": url, "model": model, "enabled": enabled == "1", "reachable": False, "models": []}
    base = LLM_BASE
    if url:
        m = re.match(r"^(https?://[^/]+)", url)
        base = m.group(1) if m else base
    try:
        r = requests.get(base + "/v1/models", timeout=5)
        out["reachable"] = r.ok
        if r.ok:
            out["models"] = [m["id"] for m in r.json().get("data", [])]
    except (requests.RequestException, ValueError):
        pass
    return jsonify(out)


@app.post("/api/llm/test")
@requires_auth
def api_llm_test():
    url = read_conf_value(OLLAMA_CONF, "OllamaChat.Url") or ""
    model = read_conf_value(OLLAMA_CONF, "OllamaChat.Model") or ""
    if "/chat/completions" not in url:
        return jsonify(ok=False, output="Test only supports OpenAI-compatible endpoints."), 400
    started = datetime.datetime.now()
    try:
        r = requests.post(url, timeout=60, json={
            "model": model, "max_tokens": 60, "stream": False,
            "messages": [{"role": "user", "content":
                          "You are a dwarf warrior in World of Warcraft. Greet a passing adventurer in one short sentence."}]})
        ms = int((datetime.datetime.now() - started).total_seconds() * 1000)
        text = r.json()["choices"][0]["message"]["content"]
        return jsonify(ok=True, output=f"{text.strip()}  ({ms} ms)")
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        return jsonify(ok=False, output=f"Request failed: {e}"), 502


@app.post("/api/service/<name>/<action>")
@requires_auth
def api_service(name, action):
    if name not in CONTROLLABLE or action not in {"start", "stop", "restart"}:
        return jsonify(ok=False, output="Not allowed"), 400
    try:
        c = docker_client.containers.get(name)
        if action != "start" and name == "ac-worldserver":
            # Save, then give the server time to log everyone out and save again on its own;
            # killing it early throws away everything since the last save.
            soap("saveall")
            time.sleep(3)
        getattr(c, action)(**({"timeout": 300} if action != "start" else {}))
    except docker.errors.DockerException as e:
        return jsonify(ok=False, output=str(e)), 500
    return jsonify(ok=True, output=f"{name}: {action} done")


@app.get("/api/logs/<name>")
@requires_auth
def api_logs(name):
    if name not in CONTAINERS:
        return jsonify(ok=False), 400
    lines = min(int(request.args.get("lines", 200)), 2000)
    try:
        raw = docker_client.containers.get(name).logs(tail=lines).decode("utf-8", "replace")
    except docker.errors.DockerException as e:
        return jsonify(ok=False, output=str(e)), 500
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    flt = request.args.get("filter", "").lower()
    if flt:
        raw = "\n".join(l for l in raw.splitlines() if flt in l.lower())
    return jsonify(ok=True, output=raw)


@app.get("/api/players")
@requires_auth
def api_players():
    show_bots = request.args.get("bots") == "1"
    rows = query(
        "SELECT c.guid, c.name, c.level, c.race, c.class, c.map, c.zone, c.totaltime, a.username, "
        "       (a.username LIKE 'RNDBOT%%' OR a.os = '') AS is_bot "
        "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
        "WHERE c.online = 1 " + ("" if show_bots else "AND a.username NOT LIKE 'RNDBOT%%' ") +
        "ORDER BY is_bot, c.name LIMIT 500")
    live = live_online_names()
    if live is not None:
        # Real accounts: trust the server, not the per-account online flag.
        extra = query(
            "SELECT c.guid, c.name, c.level, c.race, c.class, c.map, c.zone, c.totaltime, a.username, 0 AS is_bot "
            "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
            "WHERE a.username NOT LIKE 'RNDBOT%%'")
        rows = [r for r in rows if r["username"].upper().startswith("RNDBOT")] + [r for r in extra if r["name"] in live]
    for r in rows:
        r["race"] = RACES.get(r["race"], r["race"])
        r["class"] = CLASSES.get(r["class"], r["class"])
        r["map"] = MAPS.get(r["map"], f"Map {r['map']}")
        r["zone"] = zone_name(r["zone"])
        r["is_bot"] = bool(r["is_bot"])
    return jsonify(rows)


@app.get("/api/accounts")
@requires_auth
def api_accounts():
    rows = query(
        "SELECT a.id, a.username, a.last_login, a.last_ip, a.online, "
        "       COALESCE(MAX(aa.gmlevel), 0) AS gmlevel, "
        "       (SELECT COUNT(*) FROM acore_characters.characters c WHERE c.account = a.id) AS characters, "
        "       EXISTS(SELECT 1 FROM acore_auth.account_banned b WHERE b.id = a.id AND b.active = 1) AS banned "
        "FROM acore_auth.account a LEFT JOIN acore_auth.account_access aa ON aa.id = a.id "
        "WHERE a.username NOT LIKE 'RNDBOT%%' GROUP BY a.id ORDER BY a.last_login DESC")
    for r in rows:
        r["last_login"] = r["last_login"].isoformat(sep=" ") if r["last_login"] else None
        r["banned"] = bool(r["banned"])
    return jsonify(rows)


@app.post("/api/accounts")
@requires_auth
def api_account_create():
    data = request.get_json(force=True)
    user, pw = data.get("username", ""), data.get("password", "")
    if not USERNAME_RE.match(user) or not (1 <= len(pw) <= 16) or " " in pw:
        return jsonify(ok=False, output="Username 3-16 letters/digits; password up to 16 chars, no spaces."), 400
    ok, out = soap(f"account create {user} {pw}")
    return jsonify(ok=ok, output=out)


@app.post("/api/accounts/<user>/<action>")
@requires_auth
def api_account_action(user, action):
    if not USERNAME_RE.match(user):
        return jsonify(ok=False, output="Bad username"), 400
    data = request.get_json(force=True, silent=True) or {}
    if action == "password":
        pw = data.get("password", "")
        if not (1 <= len(pw) <= 16) or " " in pw:
            return jsonify(ok=False, output="Password up to 16 characters, no spaces."), 400
        ok, out = soap(f"account set password {user} {pw} {pw}")
    elif action == "gmlevel":
        lvl = int(data.get("level", 0))
        if lvl not in (0, 1, 2, 3):
            return jsonify(ok=False, output="GM level must be 0-3"), 400
        ok, out = soap(f"account set gmlevel {user} {lvl} -1")
    elif action == "ban":
        reason = re.sub(r"[^\w .,!-]", "", data.get("reason", "") or "No reason")[:80]
        ok, out = soap(f"ban account {user} {data.get('duration', '-1')} {reason}")
    elif action == "unban":
        ok, out = soap(f"unban account {user}")
    else:
        return jsonify(ok=False, output="Unknown action"), 400
    return jsonify(ok=ok, output=out)


@app.post("/api/command")
@requires_auth
def api_command():
    cmd = (request.get_json(force=True).get("command") or "").strip().lstrip(".")
    if not cmd:
        return jsonify(ok=False, output="Empty command"), 400
    ok, out = soap(cmd)
    return jsonify(ok=ok, output=out)


@app.post("/api/announce")
@requires_auth
def api_announce():
    msg = (request.get_json(force=True).get("message") or "").strip()
    if not msg:
        return jsonify(ok=False, output="Empty message"), 400
    ok, out = soap(f"announce {msg}")
    return jsonify(ok=ok, output=out or "Announced.")


@app.post("/api/backup")
@requires_auth
def api_backup():
    if not backup_lock.acquire(blocking=False):
        return jsonify(ok=False, output="A backup is already running."), 409
    try:
        try:
            path = run_backup("manual")
        except (RuntimeError, docker.errors.DockerException) as e:
            return jsonify(ok=False, output=str(e)), 500
        size = os.path.getsize(path) / 1048576
        return jsonify(ok=True, output=f"Saved {os.path.basename(path)} ({size:.1f} MB)")
    finally:
        backup_lock.release()


AUTO_BACKUP_KEEP = int(os.environ.get("AUTO_BACKUP_KEEP", "14"))
AUTO_BACKUP_HOURS = int(os.environ.get("AUTO_BACKUP_HOURS", "24"))


def run_backup(prefix):
    """Dump all databases with the database container's own mysqldump (restores cleanly into MySQL)."""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"{prefix}-{stamp}.sql.gz")
    tmp = path + ".part"
    db = docker_client.containers.get("ac-database")
    cmd = ["mysqldump", "-uroot", "--single-transaction", "--routines", "--triggers",
           "--databases", "acore_auth", "acore_characters", "acore_playerbots", "acore_world"]
    exec_id = docker_client.api.exec_create(db.id, cmd, environment={"MYSQL_PWD": DB_PASS}, stdout=True, stderr=True)["Id"]
    errors = []
    try:
        with gzip.open(tmp, "wb") as out:
            for stdout, stderr in docker_client.api.exec_start(exec_id, stream=True, demux=True):
                if stdout:
                    out.write(stdout)
                if stderr:
                    errors.append(stderr.decode("utf-8", "replace"))
        code = docker_client.api.exec_inspect(exec_id)["ExitCode"]
        if code != 0:
            raise RuntimeError("".join(errors).strip()[-500:] or f"mysqldump exited with {code}")
        os.replace(tmp, path)
        return path
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def auto_backup_loop():
    """Daily backup of all databases, keeping the newest AUTO_BACKUP_KEEP automatic ones."""
    while True:
        try:
            autos = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("auto-") and f.endswith(".sql.gz"))
            newest = os.path.getmtime(os.path.join(BACKUP_DIR, autos[-1])) if autos else 0
            if time.time() - newest > AUTO_BACKUP_HOURS * 3600 and backup_lock.acquire(blocking=False):
                try:
                    app.logger.warning("automatic backup: %s", run_backup("auto"))
                finally:
                    backup_lock.release()
                autos = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("auto-") and f.endswith(".sql.gz"))
            for old_file in autos[:-AUTO_BACKUP_KEEP]:
                os.remove(os.path.join(BACKUP_DIR, old_file))
        except Exception as e:
            app.logger.warning("automatic backup failed: %s", e)
        time.sleep(1800)


threading.Thread(target=auto_backup_loop, daemon=True).start()


@app.get("/api/backups")
@requires_auth
def api_backups():
    out = []
    for root, _, files in os.walk(BACKUP_DIR):
        for f in files:
            if f.endswith(".sql.gz"):
                p = os.path.join(root, f)
                st = os.stat(p)
                out.append({"name": os.path.relpath(p, BACKUP_DIR), "size_mb": round(st.st_size / 1048576, 1),
                            "time": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(sep=" ", timespec="minutes")})
    return jsonify(sorted(out, key=lambda b: b["time"], reverse=True))


# --------------------------------------------------------------------------- activity

@app.get("/api/activity")
@requires_auth
def api_activity():
    online = query(
        "SELECT a.id, a.username, a.last_login, a.last_ip, c.online, "
        "       c.name, c.level, c.race, c.class, c.zone, c.totaltime, c.money "
        "FROM acore_auth.account a JOIN acore_characters.characters c ON c.account = a.id "
        "WHERE a.username NOT LIKE 'RNDBOT%%' ORDER BY a.username, c.level DESC")
    live = live_online_names()
    online = [r for r in online if (r["name"] in live if live is not None else False)]
    now = datetime.datetime.now()
    accounts = {}
    for r in online:
        acc = accounts.setdefault(r["id"], {
            "username": r["username"], "ip": r["last_ip"],
            "session_min": int((now - r["last_login"]).total_seconds() // 60) if r["last_login"] else None,
            "characters": []})
        acc["characters"].append({
            "name": r["name"], "level": r["level"], "race": RACES.get(r["race"], r["race"]),
            "class": CLASSES.get(r["class"], r["class"]), "zone": zone_name(r["zone"]),
            "played_h": round(r["totaltime"] / 3600, 1), "gold": r["money"] // 10000})

    totals = query(
        "SELECT COUNT(*) AS accounts, "
        "       SUM(last_login > NOW() - INTERVAL 1 DAY) AS day, "
        "       SUM(last_login > NOW() - INTERVAL 7 DAY) AS week, "
        "       SUM(last_login > NOW() - INTERVAL 30 DAY) AS month "
        "FROM acore_auth.account WHERE username NOT LIKE 'RNDBOT%%'")[0]
    per_account = query(
        "SELECT a.username, a.last_login, COUNT(c.guid) AS chars, COALESCE(MAX(c.level), 0) AS max_level, "
        "       COALESCE(SUM(c.totaltime), 0) AS played, COALESCE(SUM(c.money), 0) AS money "
        "FROM acore_auth.account a LEFT JOIN acore_characters.characters c ON c.account = a.id "
        "WHERE a.username NOT LIKE 'RNDBOT%%' GROUP BY a.id ORDER BY played DESC")
    for r in per_account:
        r["last_login"] = r["last_login"].isoformat(sep=" ", timespec="minutes") if r["last_login"] else None
        r["played_h"] = round(int(r.pop("played")) / 3600, 1)
        r["gold"] = int(r.pop("money")) // 10000
    return jsonify(online=list(accounts.values()),
                   totals={k: int(v or 0) for k, v in totals.items()},
                   accounts=per_account)


# --------------------------------------------------------------------------- characters & alts

@app.get("/api/characters")
@requires_auth
def api_characters():
    rows = query(
        "SELECT c.name, c.level, c.race, c.class, c.zone, c.online, c.money, a.username, a.id AS account "
        "FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
        "WHERE a.username NOT LIKE 'RNDBOT%%' ORDER BY c.online DESC, a.username, c.level DESC")
    live = live_online_names()
    for r in rows:
        r["race"] = RACES.get(r["race"], r["race"])
        r["class"] = CLASSES.get(r["class"], r["class"])
        r["zone"] = zone_name(r["zone"])
        r["online"] = (r["name"] in live) if live is not None else bool(r["online"])
        r["gold"] = r.pop("money") // 10000
        if r["online"]:
            r["level"] = live_level(r["name"]) or r["level"]
    rows.sort(key=lambda r: (not r["online"], r["username"], -r["level"]))
    return jsonify(rows)


@app.post("/api/altbot")
@requires_auth
def api_altbot():
    d = request.get_json(force=True)
    master, alt, action = d.get("master", ""), d.get("alt", ""), d.get("action", "")
    if action == "regroup" and valid_char(master):
        ok, out = soap(f"dash altbot regroup {master}")
        return jsonify(ok=ok, output=out)
    if not valid_char(master) or not valid_char(alt) or action not in ("add", "remove", "invite"):
        return jsonify(ok=False, output="Bad request"), 400
    if action != "add":
        ok, out = soap(f"dash altbot {action} {master} {alt}")
        return jsonify(ok=ok, output=out or f"{alt}: {action} requested")
    return jsonify(**join_party(master, alt))


def wait_for(check, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    return None


def join_party(master, alt):
    """Log an alt in as master's bot and put it in the party, recovering from stuck logins."""
    steps = []
    ok, out = soap(f"dash altbot add {master} {alt}")
    steps.append(out)
    if not ok:
        return {"ok": False, "output": "\n".join(steps)}

    def invited():
        ok2, out2 = soap(f"dash altbot invite {master} {alt}")
        return out2 if ok2 else None

    if "already logged in" in out:
        # Already in the world: fine if it is already our bot, otherwise it is stuck or someone
        # else's bot. Kick it, wait for the logout to finish, and log it in again.
        done = invited()
        if done:
            steps.append(done)
            return {"ok": True, "output": "\n".join(steps)}
        soap(f"kick {alt}")
        steps.append(f"{alt} was stuck in the world; kicked it.")
        gone = wait_for(lambda: "is not online" in soap(f"dash altbot invite {master} {alt}")[1], 30)
        if not gone:
            steps.append(f"{alt} did not log out within 30 seconds; try again in a minute.")
            return {"ok": False, "output": "\n".join(steps)}
        time.sleep(2)
        ok, out = soap(f"dash altbot add {master} {alt}")
        steps.append(out)
        if not ok:
            return {"ok": False, "output": "\n".join(steps)}

    # Login is asynchronous; keep inviting until the bot has arrived.
    done = wait_for(invited, 20)
    if done:
        steps.append(done)
        return {"ok": True, "output": "\n".join(steps)}
    steps.append(f"{alt} did not arrive within 20 seconds.")
    return {"ok": False, "output": "\n".join(steps)}


@app.post("/api/altbot/cmd")
@requires_auth
def api_altbot_cmd():
    d = request.get_json(force=True)
    master, bot, command = d.get("master", ""), d.get("bot", ""), (d.get("command") or "").strip()
    if not valid_char(master) or not valid_char(bot) or not command or len(command) > 200 or "\n" in command:
        return jsonify(ok=False, output="Bad request"), 400
    ok, out = soap(f"dash botcmd {master} {bot} {command}")
    return jsonify(ok=ok, output=out)


@app.post("/api/altbot/ready")
@requires_auth
def api_altbot_ready():
    """Level an alt to its master's level, then let the bot sort spells/talents and gear itself."""
    d = request.get_json(force=True)
    master, bot = d.get("master", ""), d.get("bot", "")
    if not valid_char(master) or not valid_char(bot):
        return jsonify(ok=False, output="Bad request"), 400
    rows = query("SELECT name, level, online FROM acore_characters.characters WHERE name IN (%s, %s)", (master, bot))
    info = {r["name"]: r for r in rows}
    if master not in info or bot not in info:
        return jsonify(ok=False, output="Character not found"), 404
    if not char_online(bot):
        return jsonify(ok=False, output=f"{bot} must be in your party first"), 400
    steps = []
    target = live_level(master) or info[master]["level"]
    if (live_level(bot) or info[bot]["level"]) < target:
        ok, out = soap(f"character level {bot} {target}")
        steps.append(f"level {target}: {out or 'ok'}")
        if not ok:
            return jsonify(ok=False, output="\n".join(steps))
        time.sleep(1)
    # Only additive steps: learn spells, max skills, upgrade gear. The bot "maintenance" command
    # also resets and re-picks talents, which wrecks a character someone also plays by hand.
    ok, out = soap(f"dash learn {bot}", timeout=60)
    steps.append(out)
    if not ok:
        return jsonify(ok=False, output="\n".join(steps))
    ok, out = soap(f"dash autogear {bot} legendary", timeout=60)
    steps.append(out)
    if ok:
        ok, out = soap(f"dash sell {bot}", timeout=60)
        steps.append(out)
    return jsonify(ok=ok, output="\n".join(steps))


@app.post("/api/altbot/fullmaint")
@requires_auth
def api_altbot_fullmaint():
    """Level, learn, maintenance (talents/glyphs/bags/consumables) and autogear for every bot
    the master currently has logged in."""
    master = (request.get_json(force=True) or {}).get("master", "")
    if not valid_char(master):
        return jsonify(ok=False, output="Bad request"), 400
    ok, text = soap("dash who")
    players, _ = parse_who(text) if ok else ([], {})
    me = next((p for p in players if p["name"] == master), None)
    if not me:
        return jsonify(ok=False, output=f"{master} is not online (or is a bot)."), 400
    if not me["bots"]:
        return jsonify(ok=False, output=f"{master} has no bots logged in. Use Join my party first."), 400

    target = me["level"]
    report = []
    for bot in me["bots"]:
        steps = []
        level = live_level(bot) or 0
        if level < target:
            ok, out = soap(f"character level {bot} {target}")
            steps.append(f"level {target}" if ok else f"level failed: {out}")
            time.sleep(0.5)
        for label, cmd in (("learn", f"dash learn {bot}"),
                           ("maintenance", f"dash botcmd {master} {bot} maintenance"),
                           ("autogear", f"dash autogear {bot} legendary"),
                           ("sold junk", f"dash sell {bot}")):
            ok, out = soap(cmd, timeout=60)
            steps.append(label if ok else f"{label} failed: {out}")
        report.append(f"{bot}: " + ", ".join(steps))
    return jsonify(ok=True, output="\n".join(report))


def give_item(character, item, count):
    if char_online(character):
        return soap(f"dash additem {character} {item} {count}")
    return soap(f'send items {character} "Delivery" "From the server admin." {item}:{count}')


@app.post("/api/give")
@requires_auth
def api_give():
    d = request.get_json(force=True)
    character = d.get("character", "")
    try:
        item, count = int(d.get("item")), int(d.get("count", 1))
    except (TypeError, ValueError):
        return jsonify(ok=False, output="Item and count must be numbers"), 400
    if not valid_char(character) or not 1 <= count <= 1000:
        return jsonify(ok=False, output="Bad character or count"), 400
    online = char_online(character)
    if online is None:
        return jsonify(ok=False, output=f"No character named {character}"), 404
    ok, out = give_item(character, item, count)
    if ok and not online:
        out = f"{character} is offline; sent by mail. {out}"
    return jsonify(ok=ok, output=out)


@app.post("/api/gold")
@requires_auth
def api_gold():
    d = request.get_json(force=True)
    character = d.get("character", "")
    try:
        copper = int(float(d.get("gold", 0)) * 10000)
    except (TypeError, ValueError):
        return jsonify(ok=False, output="Gold must be a number"), 400
    if not valid_char(character) or copper == 0 or abs(copper) > 2_000_000_000:
        return jsonify(ok=False, output="Bad character or amount"), 400
    online = char_online(character)
    if online is None:
        return jsonify(ok=False, output=f"No character named {character}"), 404
    if online:
        ok, out = soap(f"dash addmoney {character} {copper}")
    elif copper > 0:
        ok, out = soap(f'send money {character} "Delivery" "From the server admin." {copper}')
        out = f"{character} is offline; sent by mail. {out}"
    else:
        return jsonify(ok=False, output="Can only remove gold from online characters"), 400
    return jsonify(ok=ok, output=out)


@app.post("/api/character/<action>")
@requires_auth
def api_character_action(action):
    d = request.get_json(force=True)
    character = d.get("character", "")
    if not valid_char(character):
        return jsonify(ok=False, output="Bad character name"), 400
    if action == "level":
        try:
            level = int(d.get("level"))
        except (TypeError, ValueError):
            return jsonify(ok=False, output="Level must be a number"), 400
        if not 1 <= level <= 80:
            return jsonify(ok=False, output="Level must be 1-80"), 400
        ok, out = soap(f"character level {character} {level}")
    elif action == "teleport":
        try:
            tele_id = int(d.get("location"))
        except (TypeError, ValueError):
            return jsonify(ok=False, output="Pick a location from the list"), 400
        rows = query("SELECT name FROM acore_world.game_tele WHERE id = %s", (tele_id,))
        if not rows:
            return jsonify(ok=False, output="Unknown location"), 404
        # A tele hyperlink resolves by id, so names containing spaces work too.
        ok, out = soap(f"teleport name {character} |cffffffff|Htele:{tele_id}|h[{rows[0]['name']}]|h|r")
    elif action == "autogear":
        quality = d.get("quality", "legendary")
        if quality not in ("green", "blue", "epic", "legendary"):
            return jsonify(ok=False, output="Quality must be green, blue, epic or legendary"), 400
        if not char_online(character):
            return jsonify(ok=False, output=f"{character} must be online"), 400
        ok, out = soap(f"dash autogear {character} {quality}", timeout=60)
    elif action == "learn":
        if not char_online(character):
            return jsonify(ok=False, output=f"{character} must be online"), 400
        ok, out = soap(f"dash learn {character}", timeout=60)
    elif action == "sell":
        if not char_online(character):
            return jsonify(ok=False, output=f"{character} must be online"), 400
        ok, out = soap(f"dash sell {character}", timeout=60)
    elif action == "revive":
        ok, out = soap(f"revive {character}")
    elif action == "kick":
        ok, out = soap(f"kick {character}")
    else:
        return jsonify(ok=False, output="Unknown action"), 400
    return jsonify(ok=ok, output=out or "Done")


@app.get("/api/teleports")
@requires_auth
def api_teleports():
    rows = query("SELECT id, name, map FROM acore_world.game_tele ORDER BY name")
    continents = {0, 1, 530, 571, 609}
    return jsonify([{"id": r["id"], "name": r["name"].strip(), "label": readable(r["name"].strip()), "map": map_name(r["map"]),
                     "instance": r["map"] not in continents} for r in rows])


@app.post("/api/summon")
@requires_auth
def api_summon():
    d = request.get_json(force=True)
    master, target = d.get("master", ""), d.get("target", "")
    if not valid_char(master) or not (target == "all" or valid_char(target)):
        return jsonify(ok=False, output="Bad request"), 400
    ok, out = soap(f"dash summon {master} {target}")
    return jsonify(ok=ok, output=out)


# --------------------------------------------------------------------------- items

@app.get("/api/items")
@requires_auth
def api_items():
    a = request.args
    q = a.get("q", "").strip()
    where, args = ["1=1"], []

    def num(key):
        try:
            return int(a[key]) if a.get(key, "") != "" else None
        except ValueError:
            return None

    if q.isdigit():
        where.append("entry = %s"); args.append(int(q))
    elif q:
        for word in q.split():
            where.append("name LIKE %s"); args.append(f"%{word}%")
    if a.get("junk") != "1":
        where.append(JUNK_NAME)

    cls = num("cls")
    if cls in CLASS_ARMOR:
        where.append("(AllowableClass = -1 OR AllowableClass & %s <> 0)"); args.append(1 << (cls - 1))
        armor = ",".join(str(x) for x in sorted(CLASS_ARMOR[cls] | {0}))
        weapons = ",".join(str(x) for x in sorted(CLASS_WEAPONS[cls] | {14, 20}))
        # Cloaks are cloth for everyone.
        where.append(f"(class NOT IN (2, 4) OR (class = 4 AND (subclass IN ({armor}) OR InventoryType = 16)) "
                     f"OR (class = 2 AND subclass IN ({weapons})))")

    for key, col in (("quality", "Quality"), ("slot", "InventoryType"), ("armortype", "subclass")):
        v = num(key)
        if v is not None:
            if key == "armortype":
                where.append("class = 4")
            where.append(f"{col} = %s"); args.append(v)
    kind = a.get("kind", "")
    if kind == "armor":
        where.append("class = 4")
    elif kind == "weapon":
        where.append("class = 2")
    elif kind == "gear":
        where.append("class IN (2, 4)")
    elif kind == "consumable":
        where.append("class = 0")
    elif kind == "gem":
        where.append("class = 3")
    elif kind == "bag":
        where.append("class = 1")
    elif kind == "recipe":
        where.append("class = 9")
    elif kind == "mount":
        where.append("class = 15 AND subclass = 5")
    elif kind == "pet":
        where.append("class = 15 AND subclass = 2")
    maxlvl = num("maxlvl")
    if maxlvl is not None:
        # Items without a required level (crafted trinkets, goggles, ...) still need to fit the level.
        where.append("(RequiredLevel > 0 OR ItemLevel <= %s)"); args.append(maxlvl + 10)
    for key, cond in (("minlvl", "RequiredLevel >= %s"), ("maxlvl", "RequiredLevel <= %s"),
                      ("minilvl", "ItemLevel >= %s"), ("maxilvl", "ItemLevel <= %s")):
        v = num(key)
        if v is not None:
            where.append(cond); args.append(v)
    stat = num("stat")
    if stat is not None:
        where.append("(" + " OR ".join(f"stat_type{i} = %s" for i in range(1, 11)) + ")")
        args.extend([stat] * 10)

    order = ITEM_SORTS.get(a.get("sort", ""), ITEM_SORTS["ilvl"])
    stat_cols = ", ".join(f"stat_type{i}, stat_value{i}" for i in range(1, 11))
    rows = query(
        f"SELECT entry, name, class, subclass, Quality, ItemLevel, RequiredLevel, InventoryType, armor, displayid, "
        f"       dmg_min1, dmg_max1, delay, {stat_cols} "
        f"FROM acore_world.item_template WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 200", args)
    out = []
    for r in rows:
        stats = [f"+{r[f'stat_value{i}']} {STATS.get(r[f'stat_type{i}'], '?')}"
                 for i in range(1, 11) if r[f"stat_type{i}"] and r[f"stat_value{i}"]]
        kind_name = ""
        if r["class"] == 15 and r["subclass"] == 5:
            kind_name = "Mount"
        elif r["class"] == 15 and r["subclass"] == 2:
            kind_name = "Pet"
        elif r["class"] == 4:
            kind_name = ARMOR_TYPES.get(r["subclass"], "")
        elif r["class"] == 2:
            kind_name = WEAPON_TYPES.get(r["subclass"], "")
        extra = []
        if r["armor"]:
            extra.append(f"{r['armor']} armor")
        if r["dmg_max1"]:
            dps = (r["dmg_min1"] + r["dmg_max1"]) / 2 / (r["delay"] / 1000) if r["delay"] else 0
            extra.append(f"{dps:.1f} dps")
        out.append({"entry": r["entry"], "name": r["name"], "Quality": r["Quality"],
                    "ItemLevel": r["ItemLevel"], "RequiredLevel": r["RequiredLevel"],
                    "slot": SLOTS.get(r["InventoryType"], ""), "type": kind_name,
                    "stats": ", ".join(extra + stats),
                    "icon": ICON_URL.format(size="medium", name=ITEM_ICONS.get(r["displayid"], "inv_misc_questionmark"))})
    return jsonify(out)


# --------------------------------------------------------------------------- commands

@app.get("/api/commands")
@requires_auth
def api_commands():
    q = request.args.get("q", "").strip()
    rows = query("SELECT name, security, help FROM acore_world.command WHERE name LIKE %s OR help LIKE %s "
                 "ORDER BY name LIMIT 300", (f"%{q}%", f"%{q}%"))
    for r in rows:
        r["help"] = (r["help"] or "").replace("\r\n", "\n").replace("\\n", "\n")
    return jsonify(rows)


# --------------------------------------------------------------------------- bots

@app.get("/api/bots")
@requires_auth
def api_bots():
    ok, text = soap("dash who")
    if ok:
        players, totals = parse_who(text)
        live = live_bot_breakdown(totals)
        if live:
            levels = totals["levels"]
            zones = totals["zones"]
            near = []
            for p in players:
                band = sum(n for lvl, n in levels.items() if abs(lvl - p["level"]) <= 5)
                near.append({"name": p["name"], "level": p["level"], "zone": p["zone"], "class": p.get("class"),
                             "bots_in_zone": zones.get(p["zone_id"], 0), "bots_near_level": band})
            settings = {k: read_conf_value(PLAYERBOTS_CONF, f"AiPlayerbot.{k}") for k in
                        ("MaxRandomBots", "SyncLevelWithPlayers", "RandomBotConcentrateInPlayerZone", "BotAutologin")}
            return jsonify(levels=live["levels"], zones=live["zones"], players=near, settings=settings, live=True)
    return api_bots_db()


def api_bots_db():
    base = ("FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
            "WHERE c.online = 1 AND a.username LIKE 'RNDBOT%%' ")
    levels = query("SELECT FLOOR((c.level - 1) / 10) * 10 + 1 AS bracket, COUNT(*) AS n " + base +
                   "GROUP BY bracket ORDER BY bracket")
    zones = query("SELECT c.zone, COUNT(*) AS n " + base + "GROUP BY c.zone ORDER BY n DESC LIMIT 6")
    players = query(
        "SELECT c.name, c.level, c.zone FROM acore_characters.characters c "
        "JOIN acore_auth.account a ON a.id = c.account "
        "WHERE c.online = 1 AND a.username NOT LIKE 'RNDBOT%%' AND a.os <> ''")
    near = []
    for p in players:
        n = query("SELECT COUNT(*) AS n " + base + "AND c.zone = %s", (p["zone"],))[0]["n"]
        band = query("SELECT COUNT(*) AS n " + base + "AND c.level BETWEEN %s AND %s",
                     (max(1, p["level"] - 5), p["level"] + 5))[0]["n"]
        near.append({"name": p["name"], "level": p["level"], "zone": zone_name(p["zone"]),
                     "bots_in_zone": n, "bots_near_level": band})
    settings = {k: read_conf_value(PLAYERBOTS_CONF, f"AiPlayerbot.{k}") for k in
                ("MaxRandomBots", "SyncLevelWithPlayers", "RandomBotConcentrateInPlayerZone", "BotAutologin")}
    return jsonify(levels=[{"bracket": f"{int(r['bracket'])}-{int(r['bracket']) + 9}", "n": r["n"]} for r in levels],
                   zones=[{"zone": zone_name(r["zone"]), "n": r["n"]} for r in zones],
                   players=near, settings=settings, live=False)


BOT_ACTIONS = {
    "reroll": ("dash reroll", "Random bots re-rolled (LLM agents left alone)."),
    "teleport": ("playerbots rndbot teleport", "Random bots teleported to level-appropriate zones."),
    "revive": ("playerbots rndbot revive", "Dead random bots revived."),
}


@app.post("/api/bots/<action>")
@requires_auth
def api_bots_action(action):
    if action not in BOT_ACTIONS:
        return jsonify(ok=False, output="Unknown action"), 400
    cmd, msg = BOT_ACTIONS[action]
    ok, out = soap(cmd, timeout=300)
    return jsonify(ok=ok, output=out or msg)


# --------------------------------------------------------------------------- overview

_static_cache = {}


def static_counts():
    if "world" not in _static_cache:
        row = query("SELECT (SELECT COUNT(*) FROM acore_world.creature) AS creatures, "
                    "       (SELECT COUNT(*) FROM acore_world.gameobject) AS gameobjects, "
                    "       (SELECT COUNT(*) FROM acore_world.creature_template) AS creature_types, "
                    "       (SELECT COUNT(*) FROM acore_world.quest_template) AS quests, "
                    "       (SELECT COUNT(*) FROM acore_world.item_template) AS items")[0]
        _static_cache["world"] = {k: int(v) for k, v in row.items()}
    return _static_cache["world"]


def container_stats(name):
    info = container_info(name)
    if info["status"] != "running":
        return info
    try:
        st = docker_client.containers.get(name).stats(stream=False)
        cpu = st["cpu_stats"]; pre = st["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        info["cpu"] = round(cpu_delta / sys_delta * ncpu * 100, 1) if sys_delta > 0 else 0.0
        mem = st["memory_stats"]
        used = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
        info["mem_mb"] = round(used / 1048576)
        info["mem_limit_mb"] = round(mem.get("limit", 0) / 1048576)
    except (docker.errors.DockerException, KeyError, ZeroDivisionError):
        pass
    return info


def parse_server_info(text):
    out = {}
    m = re.search(r"Connected players: (\d+)\. Characters in world: (\d+)", text)
    if m:
        out["connected"], out["in_world"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Server uptime: (.+)", text)
    if m:
        out["uptime"] = m.group(1).strip()
    m = re.search(r"Update time diff: (\d+)ms", text)
    if m:
        out["diff"] = int(m.group(1))
    m = re.search(r"Mean: (\d+)ms", text)
    if m:
        out["mean"] = int(m.group(1))
    m = re.search(r"Median: (\d+)ms", text)
    if m:
        out["median"] = int(m.group(1))
    m = re.search(r"Percentiles \(95, 99, max\): (\d+)ms, (\d+)ms, (\d+)ms", text)
    if m:
        out["p95"], out["p99"], out["max"] = (int(x) for x in m.groups())
    m = re.search(r"AzerothCore rev\. (\S+) (\S+)", text)
    if m:
        out["revision"], out["rev_date"] = m.group(1), m.group(2)
    return out


def parse_who(text):
    players, totals = [], {}
    levels, zones, factions = {}, {}, {}
    for line in text.splitlines():
        parts = line.split("\t")
        if parts[0] == "L" and len(parts) >= 3:
            levels[int(parts[1])] = int(parts[2])
        elif parts[0] == "Z" and len(parts) >= 3:
            zones[int(parts[1])] = int(parts[2])
        elif parts[0] == "F" and len(parts) >= 3:
            factions = {"alliance": int(parts[1]), "horde": int(parts[2])}
        if parts[0] == "P" and len(parts) >= 9:
            players.append({"name": parts[1], "level": int(parts[2]),
                            "class": CLASSES.get(int(parts[3]), parts[3]), "zone": zone_name(int(parts[4])), "zone_id": int(parts[4]),
                            "map": map_name(int(parts[5])), "latency": int(parts[6]), "account": int(parts[7]),
                            "bots": [b for b in parts[8].split(",") if b],
                            "autopilot": len(parts) > 9 and parts[9] == "1"})
        elif parts[0] == "T" and len(parts) >= 5:
            totals = {"random": int(parts[1]), "alts": int(parts[2]), "dead": int(parts[3]), "combat": int(parts[4])}
    totals.update(factions)
    totals["levels"] = levels
    totals["zones"] = zones
    return players, totals


def live_bot_breakdown(totals):
    """Bot level/zone/faction figures from the running server (the DB lags until the next save)."""
    levels = totals.get("levels") or {}
    if not levels:
        return None
    brackets = {}
    for lvl, n in levels.items():
        b = (lvl - 1) // 10 * 10 + 1
        brackets[b] = brackets.get(b, 0) + n
    zones = sorted((totals.get("zones") or {}).items(), key=lambda kv: -kv[1])
    return {"levels": [{"bracket": f"{b}-{b + 9}", "n": n} for b, n in sorted(brackets.items())],
            "zones": [{"zone": zone_name(z), "n": n} for z, n in zones[:6]],
            "alliance": totals.get("alliance", 0), "horde": totals.get("horde", 0), "live": True}


def parse_ollama(text):
    out = {}
    for key, pat in (("submitted", r"(\d+) submitted"), ("delivered", r"(\d+) delivered"),
                     ("failed", r"(\d+) failed"), ("queued", r"(\d+) queued"), ("in_flight", r"(\d+) in flight"),
                     ("sends_minute", r"(\d+) sends in the last minute")):
        m = re.search(pat, text)
        if m:
            out[key] = int(m.group(1))
    m = re.search(r"Last error: (.+)", text)
    out["last_error"] = m.group(1).strip() if m else None
    out["enabled"] = "Module: enabled" in text
    return out


def llm_ping():
    url = read_conf_value(OLLAMA_CONF, "OllamaChat.Url") or ""
    m = re.match(r"^(https?://[^/]+)", url)
    base = m.group(1) if m else LLM_BASE
    started = time.monotonic()
    try:
        r = requests.get(base + "/v1/models", timeout=5)
        return {"reachable": r.ok, "latency_ms": int((time.monotonic() - started) * 1000), "url": url,
                "model": read_conf_value(OLLAMA_CONF, "OllamaChat.Model")}
    except requests.RequestException:
        return {"reachable": False, "latency_ms": None, "url": url,
                "model": read_conf_value(OLLAMA_CONF, "OllamaChat.Model")}


_error_baseline = {}   # container start time -> Errors.log line count once startup noise is over


def uptime_seconds(text):
    secs = 0
    for n, unit in re.findall(r"(\d+) (day|hour|minute|second)", text or ""):
        secs += int(n) * {"day": 86400, "hour": 3600, "minute": 60, "second": 1}[unit]
    return secs


def error_log(uptime_text):
    """Errors logged since startup finished; the startup DB checks alone write thousands of lines."""
    try:
        c = docker_client.containers.get("ac-worldserver")
        started = c.attrs["State"].get("StartedAt")
        f = "/azerothcore/env/dist/logs/Errors.log"
        total = int(c.exec_run(["sh", "-c", f"wc -l < {f}"]).output.decode().strip() or 0)
        if uptime_seconds(uptime_text) < 120:
            return {"count": None, "recent": [], "note": "server starting"}
        base = _error_baseline.setdefault(started, total)
        recent = []
        if total > base:
            out = c.exec_run(["sh", "-c", f"tail -n +{base + 1} {f} | tail -n 8"]).output
            recent = [re.sub(r"\x1b\[[0-9;]*m", "", l)[:200] for l in out.decode("utf-8", "replace").splitlines()]
        return {"count": total - base, "recent": recent}
    except (docker.errors.DockerException, ValueError, KeyError):
        return {"count": None, "recent": []}


def bot_breakdown():
    base = ("FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
            "WHERE c.online = 1 AND a.username LIKE 'RNDBOT%%' ")
    levels = query("SELECT FLOOR((c.level - 1) / 10) * 10 + 1 AS b, COUNT(*) AS n " + base + "GROUP BY b ORDER BY b")
    zones = query("SELECT c.zone, COUNT(*) AS n " + base + "GROUP BY c.zone ORDER BY n DESC LIMIT 6")
    factions = query("SELECT SUM(c.race IN (1,3,4,7,11)) AS alliance, SUM(c.race IN (2,5,6,8,10)) AS horde " + base)[0]
    return {"levels": [{"bracket": f"{int(r['b'])}-{int(r['b']) + 9}", "n": r["n"]} for r in levels],
            "zones": [{"zone": zone_name(r["zone"]), "n": r["n"]} for r in zones],
            "alliance": int(factions["alliance"] or 0), "horde": int(factions["horde"] or 0)}


def db_overview():
    sizes = query("SELECT table_schema AS db, ROUND(SUM(data_length + index_length) / 1048576) AS mb "
                  "FROM information_schema.tables WHERE table_schema LIKE 'acore%%' GROUP BY table_schema")
    acc = query("SELECT COUNT(*) AS accounts, SUM(last_login > NOW() - INTERVAL 7 DAY) AS week "
                "FROM acore_auth.account WHERE username NOT LIKE 'RNDBOT%%'")[0]
    chars = query("SELECT COUNT(*) AS n FROM acore_characters.characters c JOIN acore_auth.account a "
                  "ON a.id = c.account WHERE a.username NOT LIKE 'RNDBOT%%'")[0]["n"]
    return {"sizes": {r["db"]: int(r["mb"]) for r in sizes}, "accounts": int(acc["accounts"]),
            "active_week": int(acc["week"] or 0), "characters": int(chars)}


def backup_overview():
    latest = None
    for root, _, files in os.walk(BACKUP_DIR):
        for f in files:
            if f.endswith(".sql.gz"):
                p = os.path.join(root, f)
                mtime = os.path.getmtime(p)
                if not latest or mtime > latest[1]:
                    latest = (os.path.relpath(p, BACKUP_DIR), mtime, os.path.getsize(p))
    du = shutil.disk_usage(BACKUP_DIR)
    return {"latest": latest[0] if latest else None,
            "latest_time": datetime.datetime.fromtimestamp(latest[1]).isoformat(sep=" ", timespec="minutes") if latest else None,
            "latest_mb": round(latest[2] / 1048576, 1) if latest else None,
            "disk_free_gb": round(du.free / 1e9), "disk_total_gb": round(du.total / 1e9)}


def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:  # one broken panel must not blank the whole overview
        app.logger.warning("overview %s failed: %s", getattr(fn, "__name__", fn), e)
        return default


@app.get("/api/overview")
@requires_auth
def api_overview():
    jobs = {
        "server": lambda: soap("server info"),
        "who": lambda: soap("dash who"),
        "instances": lambda: soap("instance stats"),
        "ollama": lambda: soap("ollama status"),
        "llm": llm_ping,
        "bots": bot_breakdown,
        "db": db_overview,
        "backups": backup_overview,
        "world": static_counts,
    }
    for name in OVERVIEW_CONTAINERS:
        jobs["svc:" + name] = (lambda n=name: container_stats(n))
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {k: pool.submit(safe, f) for k, f in jobs.items()}
        res = {k: f.result() for k, f in futures.items()}

    world_up = bool(res["server"] and res["server"][0])
    server = parse_server_info(res["server"][1]) if world_up else {}
    errors = safe(lambda: error_log(server.get("uptime"))) if world_up else {"count": None, "recent": []}
    players, totals = parse_who(res["who"][1]) if res["who"] and res["who"][0] else ([], {})
    inst = {}
    if res["instances"]:  # this command reports failure even when it prints fine
        m = re.search(r"instances loaded: dungeons \((\d+)\), battlegrounds \((\d+)\), arenas \((\d+)\)", res["instances"][1])
        if m:
            inst = dict(zip(("dungeons", "battlegrounds", "arenas"), (int(x) for x in m.groups())))
    ollama = parse_ollama(res["ollama"][1]) if res["ollama"] and res["ollama"][0] else {}

    return jsonify(
        world_up=world_up, server=server, players=players, bot_totals=totals, instances=inst,
        llm={**(res["llm"] or {}), **ollama}, errors=errors,
        bots=live_bot_breakdown(totals) or res["bots"], db=res["db"],
        backups=res["backups"], world=res["world"],
        services=[res["svc:" + n] for n in OVERVIEW_CONTAINERS],
        idle_timeout_min=60,
    )


# --------------------------------------------------------------------------- LLM agents

def llm_settings():
    return (read_conf_value(OLLAMA_CONF, "OllamaChat.Url") or f"{LLM_BASE}/v1/chat/completions",
            read_conf_value(OLLAMA_CONF, "OllamaChat.Model") or "currentmodel")


agent_runner = AgentRunner(app, query, execute, soap, llm_settings, lambda z: zone_name(z) if z else "",
                           lambda m: map_name(m), RACES, CLASSES)
agent_runner.start()


def _iso(v):
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, datetime.datetime) else v


@app.get("/api/agents")
@requires_auth
def api_agents():
    rows = query("SELECT guid, name, active, paused, sleeping, break_until, persona, goal, nudge, last_thought, "
                 "last_action, last_think, born_at, born_played FROM acore_characters.dash_agents WHERE active = 1 "
                 "ORDER BY name")
    levels = query("SELECT guid, ts, level FROM acore_characters.dash_agent_events WHERE kind IN ('levelup','born') "
                   "ORDER BY id")
    history = {}
    for r in levels:
        history.setdefault(r["guid"], []).append({"ts": _iso(r["ts"]), "level": r["level"] or 1})
    chars = {c["guid"]: c for c in query(
        "SELECT guid, race, class, gender, level, zone FROM acore_characters.characters WHERE guid IN "
        "(SELECT guid FROM acore_characters.dash_agents)")}
    counts = {}
    for c in query("SELECT guid, kind, COUNT(*) AS n, COUNT(DISTINCT zone) AS z FROM acore_characters.dash_agent_events "
                   "GROUP BY guid, kind"):
        counts.setdefault(c["guid"], {})[c["kind"]] = c["n"]
    out = []
    for r in rows:
        persona = json.loads(r["persona"]) if r["persona"] else {}
        st = agent_runner.life_time(r, agent_runner.states.get(r["guid"]) or {})
        ch = chars.get(r["guid"], {})
        cls, race = ch.get("class"), ch.get("race")
        visual = {
            "color": CLASS_COLORS.get(cls, "#888"),
            "class_icon": ICON_URL.format(size="medium", name=f"classicon_{CLASS_ICON.get(cls, 'warrior')}"),
            "portrait": ICON_URL.format(size="large", name=f"race_{RACE_ICON.get(race, 'human')}_"
                                                            f"{'female' if ch.get('gender') else 'male'}"),
        }
        mp = None
        if st.get("online") and st.get("zone_id"):
            here = map_point(st["zone_id"], st.get("x", 0), st.get("y", 0))
            trail = [map_point(z, x, y) for (_, z, x, y) in agent_runner.trails.get(r["guid"], [])
                     if z == st["zone_id"]]
            mp = {"zone": st["zone_id"], "name": st.get("zone_name"), "pos": here,
                  "trail": [t for t in trail if t],
                  "url": f"https://wow.zamimg.com/images/wow/wrath/maps/enus/original/{st['zone_id']}.jpg",
                  "fallback": f"https://wow.zamimg.com/images/wow/maps/enus/original/{st['zone_id']}.jpg"}
        c = counts.get(r["guid"], {})
        stats = {"quests": c.get("quest", 0), "deaths": c.get("death", 0), "zones": c.get("zone", 0),
                 "levelups": c.get("levelup", 0), "chats": c.get("chat", 0) + c.get("say", 0),
                 "thoughts": c.get("thought", 0), "letters": c.get("letter", 0), "journals": c.get("journal", 0),
                 "convos": c.get("convo", 0)}
        base = {"level": ch.get("level"), "race": RACES.get(race, ""), "class": CLASSES.get(cls, ""),
                "faction": "Alliance" if race in (1, 3, 4, 7, 11) else "Horde", "zone": zone_name(ch.get("zone") or 0)}
        out.append({**{k: _iso(v) for k, v in r.items() if k != "persona"}, "persona": persona, "state": st, "base": base,
                    "visual": visual, "map": mp, "stats": stats, "level_history": history.get(r["guid"], [])})
    return jsonify(enabled=agent_runner.enabled(), awake=agent_runner.world_awake, agents=out)


EQUIP_SLOTS = ["Head", "Neck", "Shoulder", "Shirt", "Chest", "Waist", "Legs", "Feet", "Wrist", "Hands",
               "Finger", "Finger", "Trinket", "Trinket", "Back", "Main Hand", "Off Hand", "Ranged", "Tabard"]


@app.get("/api/agents/<int:guid>/sheet")
@requires_auth
def api_agent_sheet(guid):
    gear = query(
        "SELECT ci.slot, it.entry, it.name, it.Quality, it.ItemLevel, it.displayid "
        "FROM acore_characters.character_inventory ci "
        "JOIN acore_characters.item_instance ii ON ii.guid = ci.item "
        "JOIN acore_world.item_template it ON it.entry = ii.itemEntry "
        "WHERE ci.guid = %s AND ci.bag = 0 AND ci.slot < 19 ORDER BY ci.slot", (guid,))
    equipment = [{"slot": EQUIP_SLOTS[g["slot"]], "slot_id": g["slot"], "entry": g["entry"], "name": g["name"],
                  "quality": g["Quality"], "ilvl": g["ItemLevel"],
                  "icon": ICON_URL.format(size="medium", name=ITEM_ICONS.get(g["displayid"], "inv_misc_questionmark"))}
                 for g in gear]
    journey = query("SELECT MIN(ts) AS first, MAX(ts) AS last, zone, MIN(level) AS level, COUNT(*) AS visits "
                    "FROM acore_characters.dash_agent_events WHERE guid = %s AND kind = 'zone' AND zone IS NOT NULL "
                    "GROUP BY zone ORDER BY first", (guid,))
    people = {}
    for m in query("SELECT about, COUNT(*) AS n, MAX(importance) AS imp, MAX(text) AS text "
                   "FROM acore_characters.dash_agent_memories m WHERE guid = %s AND about IS NOT NULL AND about <> '' "
                   "AND EXISTS (SELECT 1 FROM acore_characters.characters c WHERE c.name = m.about) "
                   "GROUP BY about", (guid,)):
        people[m["about"]] = {"name": m["about"], "memories": m["n"], "importance": m["imp"], "note": m["text"],
                              "talks": 0}
    for c in query("SELECT c.name, COUNT(*) AS n FROM acore_characters.mod_ollama_chat_history h "
                   "JOIN acore_characters.characters c ON c.guid = h.player_guid WHERE h.bot_guid = %s "
                   "GROUP BY c.name", (guid,)):
        people.setdefault(c["name"], {"name": c["name"], "memories": 0, "importance": 0, "note": "", "talks": 0})
        people[c["name"]]["talks"] = c["n"]
    born = query("SELECT born_at FROM acore_characters.dash_agents WHERE guid = %s", (guid,))
    first = query("SELECT MIN(ts) AS t FROM acore_characters.dash_agent_events WHERE guid = %s AND kind = 'born'", (guid,))
    return jsonify(equipment=equipment,
                   journey=[{**j, "first": _iso(j["first"]), "last": _iso(j["last"])} for j in journey],
                   people=sorted(people.values(), key=lambda p: -(p["talks"] * 2 + p["memories"] + p["importance"])),
                   born=_iso(first[0]["t"] if first and first[0]["t"] else (born[0]["born_at"] if born else None)))


def load_dbc(name):
    """All records of a client DBC file as tuples of uint32, plus a string-block reader."""
    try:
        with open(f"/dbc/{name}.dbc", "rb") as f:
            data = f.read()
        _, count, fields, rec_size, _ = struct.unpack("<4s4I", data[:20])
        strings = data[20 + count * rec_size:]
        rows = [struct.unpack_from(f"<{fields}I", data, 20 + i * rec_size) for i in range(count)]
        return rows, lambda o: strings[o:strings.index(b"\0", o)].decode("utf-8", "replace")
    except (OSError, struct.error, ValueError):
        return [], lambda o: ""


def _signed(v):
    return v - (1 << 32) if v >= 1 << 31 else v


def load_sheet_dbcs():
    icons_rows, s = load_dbc("SpellIcon")
    icons = {r[0]: s(r[1]).rsplit("\\", 1)[-1].lower() for r in icons_rows}
    rows, s = load_dbc("SkillLine")
    skills = {r[0]: {"name": s(r[3]), "cat": r[1], "icon": icons.get(r[37])} for r in rows}
    rows, s = load_dbc("TalentTab")
    tabs = {r[0]: {"name": s(r[1]), "class_mask": r[20], "order": r[22], "bg": s(r[23])} for r in rows}
    rows, _ = load_dbc("Talent")
    talents = {}
    for r in rows:
        for rank, spell in enumerate(r[4:13]):
            if spell:
                talents[spell] = (r[1], rank + 1)
    rows, s = load_dbc("Achievement")
    achievements = {r[0]: {"name": s(r[4]), "points": r[39], "icon": icons.get(r[42])} for r in rows}
    rows, s = load_dbc("Faction")
    factions = {r[0]: {"name": s(r[23]), "races": r[2:6], "classes": r[6:10], "base": [_signed(v) for v in r[10:14]]}
                for r in rows if _signed(r[1]) >= 0}
    return skills, tabs, talents, achievements, factions


SKILLS, TALENT_TABS, TALENTS, ACHIEVEMENTS, FACTIONS = load_sheet_dbcs()
REP_RANKS = [(42000, "Exalted"), (21000, "Revered"), (9000, "Honored"), (3000, "Friendly"), (0, "Neutral"),
             (-3000, "Unfriendly"), (-6000, "Hostile"), (-42000, "Hated")]
REP_SPAN = {"Exalted": 1000, "Revered": 21000, "Honored": 12000, "Friendly": 6000, "Neutral": 3000,
            "Unfriendly": 3000, "Hostile": 3000, "Hated": 36000}


def rep_rank(total):
    for floor, name in REP_RANKS:
        if total >= floor:
            return name, total - floor, REP_SPAN[name]
    return "Hated", 0, REP_SPAN["Hated"]


def item_row(g):
    return {"entry": g["entry"], "name": g["name"], "quality": g["Quality"], "ilvl": g["ItemLevel"],
            "count": g.get("count", 1),
            "icon": ICON_URL.format(size="medium", name=ITEM_ICONS.get(g["displayid"], "inv_misc_questionmark"))}


RPG_STATUS = {"GO_GRIND": "heading to a good spot to fight", "GO_CAMP": "heading back to town",
              "WANDER_NPC": "visiting NPCs (quests, vendors, trainers)", "WANDER_RANDOM": "exploring around",
              "IDLE": "deciding what to do next", "REST": "taking a short rest", "DO_QUEST": "working on a quest",
              "TRAVEL_FLIGHT": "on a flight", "OUTDOOR_PVP": "in world PvP"}


def autopilot_status(name):
    ok, out = soap(f"dash autopilot {name} status")
    if not ok:
        return None
    kv = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
    if kv.get("autopilot") != "on":
        return {"on": False}
    st = {"on": True, "mode": kv.get("mode"), "stats": kv.get("stats", ""),
          "since": _iso(datetime.datetime.fromtimestamp(int(kv.get("since", "0") or 0)))}
    rpg = kv.get("rpg", "").replace("Status: ", "")
    st["doing"] = RPG_STATUS.get(rpg, rpg.lower().replace("_", " "))
    if kv.get("quest", "").isdigit() and int(kv["quest"]):
        q = query("SELECT LogTitle FROM acore_world.quest_template WHERE ID = %s", (int(kv["quest"]),))
        if q:
            st["doing"] += f": {q[0]['LogTitle']}"
    return st


@app.post("/api/character/<name>/autopilot")
@requires_auth
def api_character_autopilot(name):
    mode = (request.get_json(force=True, silent=True) or {}).get("mode", "")
    if not valid_char(name) or mode not in ("quest", "grind", "off"):
        return jsonify(ok=False, output="Bad request"), 400
    ok, out = soap(f"dash autopilot {name} {mode}")
    return jsonify(ok=ok, output=out.strip())


@app.get("/api/character/search")
@requires_auth
def api_character_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    rows = query("SELECT c.name, c.level, c.class, a.username LIKE 'RNDBOT%%' AS bot FROM acore_characters.characters c "
                 "JOIN acore_auth.account a ON a.id = c.account WHERE c.name LIKE %s ORDER BY bot, c.name LIMIT 25",
                 (q.replace("%", "").replace("_", "") + "%",))
    return jsonify([{**r, "class": CLASSES.get(r["class"], "?"), "bot": bool(r["bot"])} for r in rows])


@app.get("/api/character/<name>/view")
@requires_auth
def api_character_view(name):
    """Everything about one character, for anyone on the server (players, alts, random bots, agents)."""
    rows = query("SELECT c.*, a.username FROM acore_characters.characters c "
                 "JOIN acore_auth.account a ON a.id = c.account WHERE c.name = %s", (name,))
    if not rows:
        return jsonify(error="No such character"), 404
    c = rows[0]
    guid, race, cls = c["guid"], c["race"], c["class"]
    is_rndbot = c["username"].upper().startswith("RNDBOT")
    agent = query("SELECT guid FROM acore_characters.dash_agents WHERE guid = %s", (guid,))
    who = live_who()
    live = None if who is None else set(who[0]) | who[1]
    online = bool(c["online"]) if live is None or is_rndbot else name in live
    at_client = who is not None and name in who[0]
    st = agent_runner.read_state(name) if online else None
    if not st or not st.get("online"):
        st = None

    inv = query(
        "SELECT ci.bag, ci.slot, ci.item, ii.count, it.entry, it.name, it.Quality, it.ItemLevel, it.displayid, "
        "it.class AS iclass FROM acore_characters.character_inventory ci "
        "JOIN acore_characters.item_instance ii ON ii.guid = ci.item "
        "JOIN acore_world.item_template it ON it.entry = ii.itemEntry WHERE ci.guid = %s ORDER BY ci.bag, ci.slot",
        (guid,))
    equip_bags = {r["item"] for r in inv if r["bag"] == 0 and 19 <= r["slot"] <= 22}
    bank_bags = {r["item"] for r in inv if r["bag"] == 0 and 67 <= r["slot"] <= 73}
    equipment, bags, bank, keys = [], [], [], []
    for r in inv:
        it = item_row(r)
        if r["bag"] == 0 and r["slot"] < 19:
            equipment.append({**it, "slot": EQUIP_SLOTS[r["slot"]], "slot_id": r["slot"]})
        elif (r["bag"] == 0 and 19 <= r["slot"] <= 38) or r["bag"] in equip_bags:
            bags.append(it)
        elif (r["bag"] == 0 and 39 <= r["slot"] <= 73) or r["bag"] in bank_bags:
            bank.append(it)
        elif r["bag"] == 0 and 86 <= r["slot"] <= 117:
            keys.append(it)

    skills = []
    for s in query("SELECT skill, value, max FROM acore_characters.character_skills WHERE guid = %s", (guid,)):
        info = SKILLS.get(s["skill"])
        if info and info["cat"] in (6, 8, 9, 11) and "Racial" not in info["name"]:  # weapons, armor, secondary, professions
            skills.append({"name": info["name"], "value": s["value"], "max": s["max"], "cat": info["cat"],
                           "icon": ICON_URL.format(size="medium", name=info["icon"] or "inv_misc_questionmark")})
    skills.sort(key=lambda s: ({11: 0, 9: 1, 6: 2, 8: 3}[s["cat"]], -s["value"]))

    spec = 1 << (c["activeTalentGroup"] or 0)
    points = {}
    for t in query("SELECT spell, specMask FROM acore_characters.character_talent WHERE guid = %s", (guid,)):
        if t["specMask"] & spec and t["spell"] in TALENTS:
            tab, rank = TALENTS[t["spell"]]
            points[tab] = points.get(tab, 0) + rank
    trees = sorted(((tid, tab) for tid, tab in TALENT_TABS.items() if tab["class_mask"] & (1 << (cls - 1))),
                   key=lambda x: x[1]["order"])
    talents = [{"name": tab["name"], "points": points.get(tid, 0)} for tid, tab in trees]

    quests = []
    for q in query("SELECT qs.quest, qs.status, qt.LogTitle, qt.QuestLevel FROM acore_characters.character_queststatus qs "
                   "LEFT JOIN acore_world.quest_template qt ON qt.ID = qs.quest WHERE qs.guid = %s AND qs.status IN (1, 3, 5) "
                   "ORDER BY qt.QuestLevel", (guid,)):
        quests.append({"id": q["quest"], "title": q["LogTitle"] or f"Quest {q['quest']}", "level": q["QuestLevel"],
                       "status": {1: "complete", 3: "in progress", 5: "failed"}[q["status"]]})
    done = query("SELECT COUNT(*) AS n FROM acore_characters.character_queststatus_rewarded WHERE guid = %s", (guid,))

    ach = query("SELECT achievement, date FROM acore_characters.character_achievement WHERE guid = %s "
                "ORDER BY date DESC", (guid,))
    recent = []
    for a in ach[:12]:
        info = ACHIEVEMENTS.get(a["achievement"])
        if info:
            recent.append({"id": a["achievement"], "name": info["name"], "points": info["points"],
                           "date": datetime.datetime.fromtimestamp(a["date"]).strftime("%Y-%m-%d"),
                           "icon": ICON_URL.format(size="medium", name=info["icon"] or "inv_misc_questionmark")})
    ach_points = sum(ACHIEVEMENTS.get(a["achievement"], {}).get("points", 0) for a in ach)

    reps = []
    for r in query("SELECT faction, standing, flags FROM acore_characters.character_reputation WHERE guid = %s "
                   "AND (flags & 1) AND NOT (flags & 8)", (guid,)):
        f = FACTIONS.get(r["faction"])
        if not f:
            continue
        base = 0
        for i in range(4):
            races, classes = f["races"][i], f["classes"][i]
            if (not races or races & (1 << (race - 1))) and (not classes or classes & (1 << (cls - 1))) \
                    and (races or classes):
                base = f["base"][i]
                break
        total = base + r["standing"]
        rank, cur, span = rep_rank(total)
        reps.append({"name": f["name"], "rank": rank, "value": cur, "max": span, "total": total})
    reps.sort(key=lambda r: -r["total"])

    guild = query("SELECT g.name, gr.rname FROM acore_characters.guild_member gm "
                  "JOIN acore_characters.guild g ON g.guildid = gm.guildid "
                  "LEFT JOIN acore_characters.guild_rank gr ON gr.guildid = gm.guildid AND gr.rid = gm.rank "
                  "WHERE gm.guid = %s", (guid,))
    talks = query(
        "SELECT h.timestamp AS ts, pc.name AS player, bc.name AS bot, h.player_message, h.bot_reply "
        "FROM acore_characters.mod_ollama_chat_history h "
        "LEFT JOIN acore_characters.characters pc ON pc.guid = h.player_guid "
        "LEFT JOIN acore_characters.characters bc ON bc.guid = h.bot_guid "
        "WHERE h.bot_guid = %s OR h.player_guid = %s ORDER BY h.id DESC LIMIT 20", (guid, guid))

    zone_id = st["zone_id"] if st else c["zone"]
    x, y = (st["x"], st["y"]) if st else (c["position_x"], c["position_y"])
    pos = map_point(zone_id, x, y)
    level = st["level"] if st else c["level"]
    info = {
        "guid": guid, "name": c["name"], "account": c["username"], "online": online, "live": bool(st),
        "kind": "agent" if agent else "random bot" if is_rndbot else "character",
        "level": level, "race": RACES.get(race, "?"), "class": CLASSES.get(cls, "?"),
        "gender": "female" if c["gender"] else "male",
        "faction": "Alliance" if race in (1, 3, 4, 7, 11) else "Horde",
        "zone": zone_name(zone_id), "area": st.get("area_name") if st else None, "map": map_name(st["map"] if st else c["map"]),
        "gold": st["gold"] if st else c["money"] // 10000, "money": c["money"],
        "played_h": round((st["played_s"] if st else c["totaltime"]) / 3600, 1),
        "level_played_h": round(c["leveltime"] / 3600, 1),
        "xp": st["xp"] if st else c["xp"], "xpnext": st["xpnext"] if st else None,
        "hp": st["hp"] if st else None, "alive": st["alive"] if st else None, "ilvl": st["ilvl"] if st else None,
        "group": st["group"] if st else [], "kills": c["totalKills"], "honor": c["totalHonorPoints"],
        "arena": c["arenaPoints"], "created": _iso(c["creation_date"]),
        "last_logout": _iso(datetime.datetime.fromtimestamp(c["logout_time"])) if c["logout_time"] else None,
        "guild": guild[0]["name"] if guild else None, "guild_rank": guild[0]["rname"] if guild else None,
        "quests_done": done[0]["n"] if done else 0, "achievements": len(ach), "achievement_points": ach_points,
    }
    if not info["ilvl"]:
        lv = [e["ilvl"] for e in equipment if e["slot_id"] not in (3, 18)]
        info["ilvl"] = round(sum(lv) / len(lv)) if lv else None
    visual = {
        "color": CLASS_COLORS.get(cls, "#888"),
        "class_icon": ICON_URL.format(size="medium", name=f"classicon_{CLASS_ICON.get(cls, 'warrior')}"),
        "portrait": ICON_URL.format(size="large", name=f"race_{RACE_ICON.get(race, 'human')}_{info['gender']}"),
    }
    mp = {"zone": zone_id, "name": zone_name(zone_id), "pos": pos, "trail": [], "live": bool(st),
          "url": f"https://wow.zamimg.com/images/wow/wrath/maps/enus/original/{zone_id}.jpg",
          "fallback": f"https://wow.zamimg.com/images/wow/maps/enus/original/{zone_id}.jpg"} if pos else None
    info["autopilot"] = autopilot_status(name) if at_client else None
    return jsonify(info=info, visual=visual, map=mp, equipment=equipment, bags=bags, bank=bank, keys=keys,
                   skills=skills, talents=talents, quests=quests, live_quests=st["quests"] if st else None,
                   achievements=recent, reputations=reps[:30],
                   talks=[{**t, "ts": _iso(t["ts"])} for t in talks])



@app.get("/api/agents/feed")
@requires_auth
def api_agents_feed():
    guid = request.args.get("guid")
    kinds = request.args.get("kinds", "")
    where, args = ["1=1"], []
    if guid:
        where.append("e.guid = %s"); args.append(int(guid))
    if kinds:
        ks = [k for k in kinds.split(",") if re.match(r"^[a-z]+$", k)]
        if ks:
            where.append("e.kind IN (" + ",".join(["%s"] * len(ks)) + ")"); args += ks
    limit = min(int(request.args.get("limit", 80)), 500)
    rows = query("SELECT e.id, e.guid, a.name, e.ts, e.kind, e.level, e.zone, e.text "
                 "FROM acore_characters.dash_agent_events e JOIN acore_characters.dash_agents a ON a.guid = e.guid "
                 f"WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT {limit}", args)
    return jsonify([{**r, "ts": _iso(r["ts"])} for r in rows])


@app.get("/api/agents/<int:guid>/memories")
@requires_auth
def api_agent_memories(guid):
    rows = query("SELECT ts, importance, about, text FROM acore_characters.dash_agent_memories WHERE guid = %s "
                 "ORDER BY importance DESC, ts DESC", (guid,))
    return jsonify([{**r, "ts": _iso(r["ts"])} for r in rows])


@app.post("/api/agents/<int:guid>/<action>")
@requires_auth
def api_agent_action(guid, action):
    d = request.get_json(force=True, silent=True) or {}
    rows = query("SELECT name FROM acore_characters.dash_agents WHERE guid = %s", (guid,))
    if not rows:
        return jsonify(ok=False, output="Unknown agent"), 404
    name = rows[0]["name"]
    if action in ("pause", "resume"):
        execute("UPDATE acore_characters.dash_agents SET paused = %s WHERE guid = %s", (action == "pause", guid))
        agent_runner.event(guid, "status", f"{name} was {'paused' if action == 'pause' else 'resumed'} by the admin.")
        return jsonify(ok=True, output=f"{name} {action}d")
    if action == "think":
        agent_runner.think_now.add(guid)
        return jsonify(ok=True, output=f"{name} will think within a few seconds.")
    if action == "nudge":
        text = re.sub(r"\s+", " ", d.get("text", "")).strip()[:300]
        if not text:
            return jsonify(ok=False, output="Empty nudge"), 400
        execute("UPDATE acore_characters.dash_agents SET nudge = %s WHERE guid = %s", (text, guid))
        agent_runner.event(guid, "nudge", f"Admin nudge: {text}")
        agent_runner.think_now.add(guid)
        return jsonify(ok=True, output=f"{name} will consider it.")
    if action == "newlife":
        execute("UPDATE acore_characters.dash_agents SET born_at = NULL, born_played = NULL, persona = NULL, goal = NULL, "
                "last_thought = NULL, last_action = NULL WHERE guid = %s", (guid,))
        execute("DELETE FROM acore_characters.dash_agent_memories WHERE guid = %s", (guid,))
        agent_runner.event(guid, "status", f"{name}'s old life ends. A new one begins.")
        agent_runner.think_now.add(guid)
        return jsonify(ok=True, output=f"{name} starts a brand new life at level 1.")
    return jsonify(ok=False, output="Unknown action"), 400


@app.post("/api/agents/enabled")
@requires_auth
def api_agents_enabled():
    on = bool((request.get_json(force=True, silent=True) or {}).get("on"))
    execute("REPLACE INTO acore_characters.dash_settings (k, v) VALUES ('agents_enabled', %s)", ("1" if on else "0",))
    return jsonify(ok=True, output="Agents are " + ("thinking" if on else "paused"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
