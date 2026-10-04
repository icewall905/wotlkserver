"""LLM agents: bots whose life decisions are made by a language model.

Playerbots plays the character (combat, questing, movement). Every few minutes each agent's
"mind" looks at its situation, memories and recent events, and decides what to do next from a
small set of high-level actions that the mod-dashboard-tools module carries out in game.
Everything an agent thinks, says and does is written to dash_agent_events so it can be
watched in the dashboard.
"""
import collections
import json
import zlib
import random
import re
import threading
import time

import requests

THINK_INTERVAL = (120, 200)          # seconds between an agent's decisions
INVITE_COOLDOWN = 30 * 60            # per agent and target player
GOTO_COOLDOWN = 45 * 60              # per agent and target player
TRAVEL_COOLDOWN = 20 * 60            # per agent
MAX_MEMORIES = 60
LETTER_TARGET_COOLDOWN = 6 * 3600
LETTER_AGENT_COOLDOWN = 3 * 3600
CONVO_PAIR_COOLDOWN = 30 * 60
SIGHTSEE_COOLDOWN = 60 * 60
FISH_COOLDOWN = 30 * 60

# Places worth a sightseeing trip: (label, min level, faction A/H/B, game_tele name)
LANDMARKS = [
    ("Stormwind City", 1, "A", "Stormwind"), ("Ironforge", 1, "A", "Ironforge"), ("Darnassus", 1, "A", "Darnassus"),
    ("The Exodar", 1, "A", "TheExodar"), ("Goldshire inn", 1, "A", "Goldshire"), ("Menethil Harbor", 15, "A", "MenethilHarbor"),
    ("Orgrimmar", 1, "H", "Orgrimmar"), ("Undercity", 1, "H", "Undercity"), ("Thunder Bluff", 1, "H", "ThunderBluff"),
    ("Silvermoon City", 1, "H", "SilvermoonCity"), ("Booty Bay", 30, "B", "BootyBay"), ("Gadgetzan", 40, "B", "Gadgetzan"),
    ("Ratchet", 12, "B", "Ratchet"), ("Moonglade", 15, "B", "Moonglade"), ("Shattrath City", 58, "B", "Shattrath"),
    ("Dalaran", 68, "B", "Dalaran"),
]

# (label, min level, max level, faction A/H/B, game_tele name)
ZONES = [
    ("Northshire Valley", 1, 6, "A", "NorthshireValley"), ("Elwynn Forest", 5, 12, "A", "Goldshire"),
    ("Dun Morogh", 1, 12, "A", "Kharanos"), ("Teldrassil", 1, 12, "A", "Dolanaar"),
    ("Azuremyst Isle", 1, 12, "A", "AzuremystIsle"), ("Westfall", 10, 20, "A", "SentinelHill"),
    ("Loch Modan", 10, 20, "A", "Thelsamar"), ("Darkshore", 11, 20, "A", "Auberdine"),
    ("Bloodmyst Isle", 10, 20, "A", "BloodmystIsle"), ("Redridge Mountains", 15, 25, "A", "Lakeshire"),
    ("Duskwood", 20, 30, "A", "Darkshire"), ("Wetlands", 20, 30, "A", "MenethilHarbor"),
    ("Ashenvale", 18, 30, "B", "Astranaar"), ("Stonetalon Mountains", 15, 27, "B", "StonetalonMountains"),
    ("Hillsbrad Foothills", 20, 30, "B", "HillsbradFoothills"), ("Thousand Needles", 25, 35, "B", "ThousandNeedles"),
    ("Arathi Highlands", 30, 40, "B", "ArathiHighlands"), ("Stranglethorn Vale", 30, 45, "B", "BootyBay"),
    ("Desolace", 30, 40, "B", "Desolace"), ("Dustwallow Marsh", 35, 45, "B", "DustwallowMarsh"),
    ("Badlands", 35, 45, "B", "Badlands"), ("Swamp of Sorrows", 35, 45, "B", "SwampOfSorrows"),
    ("Feralas", 40, 50, "B", "Feralas"), ("Tanaris", 40, 50, "B", "Gadgetzan"),
    ("Searing Gorge", 43, 50, "B", "SearingGorge"), ("Felwood", 48, 55, "B", "Felwood"),
    ("Un'Goro Crater", 48, 55, "B", "UnGoroCrater"), ("Azshara", 45, 55, "B", "Azshara"),
    ("Burning Steppes", 50, 58, "B", "BurningSteppes"), ("Western Plaguelands", 51, 58, "B", "WesternPlaguelands"),
    ("Eastern Plaguelands", 53, 60, "B", "EasternPlaguelands"), ("Winterspring", 53, 60, "B", "Winterspring"),
    ("Silithus", 55, 60, "B", "Silithus"), ("Hellfire Peninsula", 58, 63, "B", "HellfirePeninsula"),
    ("Zangarmarsh", 60, 64, "B", "Zangarmarsh"), ("Terokkar Forest", 62, 65, "B", "TerokkarForest"),
    ("Nagrand", 64, 67, "B", "Nagrand"), ("Blade's Edge Mountains", 65, 68, "B", "BladesEdgeMountains"),
    ("Netherstorm", 67, 70, "B", "Netherstorm"), ("Shadowmoon Valley", 67, 70, "B", "ShadowmoonVillage"),
    ("Borean Tundra", 68, 72, "B", "BoreanTundra"), ("Howling Fjord", 68, 72, "B", "HowlingFjord"),
    ("Dragonblight", 71, 74, "B", "Dragonblight"), ("Grizzly Hills", 73, 75, "B", "GrizzlyHills"),
    ("Zul'Drak", 74, 77, "B", "ZulDrak"), ("Sholazar Basin", 76, 78, "B", "SholazarBasin"),
    ("The Storm Peaks", 77, 80, "B", "StormPeaks"), ("Icecrown", 77, 80, "B", "IcecrownGlacier"),
    ("Durotar", 1, 12, "H", "RazorHill"), ("Mulgore", 1, 12, "H", "Mulgore"),
    ("Tirisfal Glades", 1, 12, "H", "Brill"), ("Eversong Woods", 1, 12, "H", "EversongWoods"),
    ("The Barrens", 10, 25, "H", "TheBarrens"), ("Silverpine Forest", 10, 20, "H", "SilverpineForest"),
    ("Ghostlands", 10, 20, "H", "Ghostlands"),
]

ACTIONS_DOC = """Choose exactly one action:
- {"type":"continue"}  keep doing what your body is doing (questing/fighting); the usual choice
- {"type":"focus","mode":"quest"|"grind"}  questing vs. hunting monsters for experience
- {"type":"travel","location":<id from LOCATIONS>}  move on to a new region suited to your level
- {"type":"train"}  visit your class trainer: learn new spells for your level
- {"type":"gear_up"}  sort out your equipment (buy/upgrade gear for your level)
- {"type":"goto_player","name":"<real player>"}  go and find a real player you want to meet
- {"type":"invite","name":"<real player nearby, same faction>"}  invite a real player to adventure together
- {"type":"whisper","name":"<player>","text":"..."}  send a private message
- {"type":"group_with","name":"<other agent, same faction>"}  team up with a fellow agent
- {"type":"leave_group"}  go your own way again
- {"type":"seek_agent","name":"<other agent>"}  go and find a fellow agent you want to see
- {"type":"take_break","minutes":5-30}  step away for a bit: sit down, grab a snack, stretch (only when you've played a while)
- {"type":"send_letter","name":"<a real player you know>","subject":"...","text":"...","gold":0-5}  mail a personal letter, optionally with a little gold
- {"type":"sightsee","place":<id from LANDMARKS>}  visit a famous place just because you want to
- {"type":"go_fishing"}  relax with a fishing rod (if you like fishing)
- {"type":"buy_mount"}  buy riding training and your first (level 20) or epic (level 40) mount, if you can afford it"""


def _strip(text, n):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:n]


class AgentRunner:
    def __init__(self, app, query, execute, soap, llm_settings, zone_name, map_name, races, classes):
        self.app = app
        self.query = query            # SELECT -> list of dicts
        self.execute = execute        # INSERT/UPDATE
        self.soap = soap
        self.llm_settings = llm_settings
        self.zone_name = zone_name
        self.map_name = map_name
        self.races = races
        self.classes = classes
        self.states = {}              # guid -> last parsed state
        self.cooldowns = {}           # (guid, kind, target) -> monotonic time when allowed again
        self.next_think = {}          # guid -> monotonic time
        self.think_now = set()
        self.lock = threading.RLock()
        self.zone_ids = {}
        self.last_chat = {}
        self.world_awake = None
        self.landmark_ids = {}
        self.talking = set()              # agent names currently in a conversation
        self.just_woke = set()
        self.reload_chat_due = False
        self.last_chat_reload = 0
        self.trails = collections.defaultdict(lambda: collections.deque(maxlen=120))   # guid -> (ts, zone, x, y)

    # ------------------------------------------------------------------ helpers

    def setting(self, key, default=None):
        rows = self.query("SELECT v FROM acore_characters.dash_settings WHERE k = %s", (key,))
        return rows[0]["v"] if rows else default

    def enabled(self):
        return self.setting("agents_enabled", "1") == "1"

    def event(self, guid, kind, text, state=None):
        state = state or self.states.get(guid) or {}
        self.execute("INSERT INTO acore_characters.dash_agent_events (guid, kind, level, zone, text) "
                     "VALUES (%s, %s, %s, %s, %s)",
                     (guid, kind, state.get("level"), state.get("zone_name"), _strip(text, 2000)))

    def remember(self, guid, text, importance=5, about=None):
        self.execute("INSERT INTO acore_characters.dash_agent_memories (guid, importance, about, text) "
                     "VALUES (%s, %s, %s, %s)", (guid, max(1, min(10, int(importance))), about, _strip(text, 400)))
        # Forget the least important, oldest memories beyond the cap.
        self.execute(
            "DELETE FROM acore_characters.dash_agent_memories WHERE guid = %s AND id NOT IN ("
            "SELECT id FROM (SELECT id FROM acore_characters.dash_agent_memories WHERE guid = %s "
            "ORDER BY importance DESC, ts DESC LIMIT %s) keep)", (guid, guid, MAX_MEMORIES))

    def cooldown_ok(self, guid, kind, target, seconds):
        key = (guid, kind, (target or "").lower())
        now = time.monotonic()
        if self.cooldowns.get(key, 0) > now:
            return False
        self.cooldowns[key] = now + seconds
        return True

    def resolve_zones(self):
        if self.zone_ids:
            return
        rows = self.query("SELECT id, name FROM acore_world.game_tele")
        by_name = {r["name"].strip().lower(): r["id"] for r in rows}
        for i, (label, lo, hi, fac, tele) in enumerate(ZONES):
            if tele.lower() in by_name:
                self.zone_ids[i] = by_name[tele.lower()]
        for i, (label, lo, fac, tele) in enumerate(LANDMARKS):
            if tele.lower() in by_name:
                self.landmark_ids[i] = by_name[tele.lower()]

    def locations_for(self, level, faction):
        fac = "A" if faction == "Alliance" else "H"
        out = []
        for i, (label, lo, hi, zf, _) in enumerate(ZONES):
            if i in self.zone_ids and zf in (fac, "B") and lo - 2 <= level <= hi + 1:
                out.append({"id": i, "name": label, "levels": f"{lo}-{hi}"})
        return out

    # ------------------------------------------------------------------ game state

    def read_state(self, name):
        ok, text = self.soap(f"dash agent state {name}")
        if not ok:
            return None
        st = {}
        for line in text.splitlines():
            if "\t" in line:
                k, v = line.split("\t", 1)
                st[k.strip()] = v.strip()
        if st.get("online") != "1":
            return {"online": False}
        num = lambda k: int(st.get(k, "0") or 0)
        state = {
            "online": True, "level": num("level"), "xp": num("xp"), "xpnext": num("xpnext"),
            "race": self.races.get(num("race"), "?"), "class": self.classes.get(num("class"), "?"),
            "faction": st.get("faction"), "zone_id": num("zone"),
            "zone_name": self.zone_name(num("zone")), "area_name": self.zone_name(num("area")),
            "map_name": self.map_name(num("map")), "hp": num("hp"), "alive": st.get("alive") == "1",
            "combat": st.get("combat") == "1", "gold": num("money") // 10000, "freebag": num("freebag"),
            "ilvl": num("ilvl"), "played_s": num("played"), "played_h": round(num("played") / 3600, 1),
            "map": num("map"), "area_id": num("area"), "riding": num("riding"), "sitting": st.get("sitting") == "1",
            "x": float((st.get("pos") or "0,0,0").split(",")[0]), "y": float((st.get("pos") or "0,0,0").split(",")[1]),
            "group": [g for g in st.get("group", "").split(",") if g],
            "quests": [q for q in st.get("quests", "").split("|") if q],
            "nearby": [],
        }
        for item in [n for n in st.get("nearby", "").split(",") if n]:
            parts = item.split(":")
            if len(parts) == 4:
                state["nearby"].append({"name": parts[0], "level": int(parts[1]), "dist": int(parts[2]),
                                        "kind": parts[3]})
        return state

    def note_conversations(self, guid, state):
        """Copy new chat lines with this agent (from the chat module's history) into the timeline."""
        last = self.last_chat.get(guid)
        if last is None:
            rows = self.query("SELECT COALESCE(MAX(id), 0) AS m FROM acore_characters.mod_ollama_chat_history "
                              "WHERE bot_guid = %s", (guid,))
            self.last_chat[guid] = rows[0]["m"]
            return
        rows = self.query(
            "SELECT h.id, c.name, h.player_message, h.bot_reply FROM acore_characters.mod_ollama_chat_history h "
            "LEFT JOIN acore_characters.characters c ON c.guid = h.player_guid "
            "WHERE h.bot_guid = %s AND h.id > %s ORDER BY h.id LIMIT 20", (guid, last))
        for r in rows:
            who = r["name"] or "someone"
            self.event(guid, "chat", f"{who}: \"{_strip(r['player_message'], 300)}\" / replied: "
                                     f"\"{_strip(r['bot_reply'], 300)}\"", state)
            self.last_chat[guid] = r["id"]

    def note_changes(self, guid, name, old, new):
        if not old or not old.get("online") or not new.get("online"):
            return
        if new["level"] > old["level"]:
            self.event(guid, "levelup", f"{name} reached level {new['level']}!", new)
            if new["level"] % 10 == 0 or new["level"] in (5, 58, 68, 80):
                self.remember(guid, f"I reached level {new['level']} in {new['zone_name']}.", 7)
        if new["zone_id"] != old["zone_id"] and new["zone_name"]:
            self.event(guid, "zone", f"Arrived in {new['zone_name']}.", new)
        if old["alive"] and not new["alive"]:
            self.event(guid, "death", f"Died in {new['area_name'] or new['zone_name']}.", new)
        old_q = {q.rsplit(" [", 1)[0]: q.endswith("[done]") for q in old["quests"]}
        new_q = {q.rsplit(" [", 1)[0] for q in new["quests"]}
        for title, done in old_q.items():
            if title not in new_q and done:
                self.event(guid, "quest", f"Completed the quest \"{title}\".", new)
        if new["ilvl"] >= old["ilvl"] + 3:
            self.event(guid, "gear", f"Better gear: item level {old['ilvl']} -> {new['ilvl']}.", new)
        for mark in (1, 10, 50, 100, 500, 1000, 5000):
            if old["gold"] < mark <= new["gold"]:
                self.event(guid, "gold", f"Now has {new['gold']} gold.", new)
                break
        if set(new["group"]) != set(old["group"]):
            joined = [g.split(":")[0] for g in new["group"] if g not in old["group"]]
            left = [g.split(":")[0] for g in old["group"] if g not in new["group"]]
            if joined:
                self.event(guid, "group", f"Now adventuring with {', '.join(joined)}.", new)
                for who in joined:
                    self.remember(guid, f"I teamed up with {who} in {new['zone_name']}.", 6, who)
            if left:
                self.event(guid, "group", f"Parted ways with {', '.join(left)}.", new)

    # ------------------------------------------------------------------ the mind

    def llm(self, messages, max_tokens=600):
        url, model = self.llm_settings()
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.85,
                "response_format": {"type": "json_object"}}
        r = requests.post(url, json=body, timeout=120)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
        text = text.split("</think>")[-1]
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0) if m else text)

    def ensure_persona(self, agent, state):
        if agent["persona"]:
            return json.loads(agent["persona"])
        prompt = (f"Invent a World of Warcraft character for a living-world experiment. Name: {agent['name']}, "
                  f"a {state['race']} {state['class']} of the {state['faction']}. Write JSON with keys: "
                  "personality (2 sentences, vivid and specific), background (2 sentences), "
                  "ambition (what they want from life in Azeroth), quirks (one habit or speech quirk), "
                  "first_goal (their first concrete goal as a fresh level 1 adventurer).")
        persona = self.llm([{"role": "user", "content": prompt}], 500)
        self.execute("UPDATE acore_characters.dash_agents SET persona = %s, goal = %s WHERE guid = %s",
                     (json.dumps(persona), _strip(persona.get("first_goal"), 300), agent["guid"]))
        self.event(agent["guid"], "born", f"{agent['name']} is born: {persona.get('personality', '')} "
                                           f"Ambition: {persona.get('ambition', '')}", state)
        self.remember(agent["guid"], f"My ambition: {persona.get('ambition', '')}", 9)
        return persona

    def upgrade_persona(self, agent, state, persona):
        """Add the traits that make an agent feel human: chat style, daily schedule, hobbies."""
        if all(k in persona for k in ("chat_style", "schedule", "hobbies")):
            return persona
        prompt = (f"Here is a World of Warcraft character: {json.dumps(persona)}. Name {agent['name']}, "
                  f"{state['race']} {state['class']}. Add human details as JSON with keys: "
                  "chat_style (how this person types in game chat: capitalisation, slang like lol/brb/ty, emotes, "
                  "typos, sentence length; be specific and consistent with the personality), "
                  "schedule ({\"type\": \"early bird\"|\"night owl\"|\"steady\", \"wake\": hour 0-23 when they "
                  "usually log on, \"sleep\": hour 0-23 when they usually log off; play 12-16 hours a day}), "
                  "hobbies (2-3 things they enjoy besides levelling, from: fishing, sightseeing, collecting gear, "
                  "making friends, writing letters, exploring, duelling, helping newbies), "
                  "dream (a big long-term dream in Azeroth).")
        extra = self.llm([{"role": "user", "content": prompt}], 400)
        sched = extra.get("schedule") or {}
        try:
            sched = {"type": str(sched.get("type", "steady")), "wake": int(sched.get("wake", 8)) % 24,
                     "sleep": int(sched.get("sleep", 23)) % 24}
        except (TypeError, ValueError):
            sched = {"type": "steady", "wake": 8, "sleep": 23}
        persona.update({"chat_style": _strip(extra.get("chat_style"), 300) or "casual, normal capitalisation",
                        "schedule": sched,
                        "hobbies": [_strip(h, 30) for h in (extra.get("hobbies") or [])][:3],
                        "dream": _strip(extra.get("dream"), 200)})
        self.execute("UPDATE acore_characters.dash_agents SET persona = %s WHERE guid = %s",
                     (json.dumps(persona), agent["guid"]))
        self.event(agent["guid"], "status", f"{agent['name']} plays {sched['wake']:02d}:00-{sched['sleep']:02d}:00 "
                                             f"({sched['type']}), enjoys {', '.join(persona['hobbies'])}. "
                                             f"Types like: {persona['chat_style']}", state)
        self.sync_chat_persona(agent, persona)
        return persona

    def sync_chat_persona(self, agent, persona):
        """Give the chat module (which answers when people talk to a bot) this agent's real persona."""
        guid, name = agent["guid"], agent["name"]
        mems = self.query("SELECT text FROM acore_characters.dash_agent_memories WHERE guid = %s "
                          "ORDER BY importance DESC, ts DESC LIMIT 8", (guid,))
        goal = self.query("SELECT goal FROM acore_characters.dash_agents WHERE guid = %s", (guid,))
        prompt = (f"You are {name}. Personality: {persona.get('personality', '')} Background: "
                  f"{persona.get('background', '')} Quirk: {persona.get('quirks', '')} "
                  f"Dream: {persona.get('dream', '')} Current goal: {(goal[0]['goal'] if goal else '') or ''}. "
                  f"Mood lately: {persona.get('mood', 'fine')}. "
                  + (f"How you have changed: {' '.join(persona.get('growth', [])[-2:])} " if persona.get("growth") else "")
                  + f"You type in chat like this: {persona.get('chat_style', 'casual')}. "
                  f"Things you remember: {' | '.join(m['text'] for m in mems)}. Stay this person in every reply.")
        key = f"AGENT_{name.upper()}"
        self.execute("REPLACE INTO acore_characters.mod_ollama_chat_personality_templates (`key`, prompt, manual_only) "
                     "VALUES (%s, %s, 1)", (key, _strip(prompt, 3000)))
        self.execute("REPLACE INTO acore_characters.mod_ollama_chat_personality (guid, personality) VALUES (%s, %s)",
                     (guid, key))
        self.reload_chat_due = True

    def local_hour(self):
        t = time.localtime()
        return t.tm_hour + t.tm_min / 60.0

    def awake_window(self, guid, persona):
        """Today's log-on/log-off hours, with up to +-40 minutes of day-to-day variation."""
        sched = persona.get("schedule") or {"wake": 8, "sleep": 23}
        day = time.strftime("%Y%m%d")
        jitter = lambda salt: ((zlib.crc32(f"{day}:{guid}:{salt}".encode()) % 81) - 40) / 60.0
        return (sched["wake"] + jitter("w")) % 24, (sched["sleep"] + jitter("s")) % 24

    def should_be_awake(self, guid, persona):
        if "schedule" not in persona:
            return True
        wake, sleep = self.awake_window(guid, persona)
        h = self.local_hour()
        return (wake <= h < sleep) if wake < sleep else (h >= wake or h < sleep)

    def say(self, agent, state, text):
        text = _strip(text, 200)
        if text and state.get("online") and state.get("alive"):
            self.soap(f"dash agent say {agent['name']} {text}")
            self.event(agent["guid"], "say", text, state)

    def go_to_sleep(self, agent, state, persona):
        name, guid = agent["name"], agent["guid"]
        parting = None
        try:
            parting = self.reflect(agent, state, persona)
        except Exception as e:
            self.app.logger.warning("reflect %s: %s", name, e)
        if parting:
            self.say(agent, state, parting)
            time.sleep(4)
        self.execute("UPDATE acore_characters.dash_agents SET sleeping = 1, break_until = NULL WHERE guid = %s", (guid,))
        self.soap("dash agent reload")
        self.soap(f"dash agent sleep {name}")
        wake, _ = self.awake_window(guid, persona)
        self.event(guid, "sleep", f"{name} logs off for the night (back around {int(wake):02d}:{int(wake % 1 * 60):02d}).", state)

    def wake_up(self, agent, persona):
        name, guid = agent["name"], agent["guid"]
        self.execute("UPDATE acore_characters.dash_agents SET sleeping = 0 WHERE guid = %s", (guid,))
        self.soap("dash agent reload")
        self.soap(f"dash agent wake {name}")
        self.event(guid, "wake", f"{name} logs on for the day.")
        self.just_woke.add(guid)
        self.next_think[guid] = time.monotonic() + 45

    def reflect(self, agent, state, persona):
        """End-of-day reflection: journal, mood, growth, opinions; returns a goodnight line."""
        guid, name = agent["guid"], agent["name"]
        since = self.query("SELECT MAX(ts) AS t FROM acore_characters.dash_agent_events WHERE guid = %s AND kind = 'journal'",
                           (guid,))[0]["t"]
        rows = self.query("SELECT ts, kind, text FROM acore_characters.dash_agent_events WHERE guid = %s "
                          "AND kind NOT IN ('status','error','thought') AND ts > COALESCE(%s, NOW() - INTERVAL 1 DAY) "
                          "ORDER BY id DESC LIMIT 70", (guid, since))
        thoughts = self.query("SELECT text FROM acore_characters.dash_agent_events WHERE guid = %s AND kind = 'thought' "
                              "ORDER BY id DESC LIMIT 12", (guid,))
        day = "\n".join(f"{r['ts'].strftime('%H:%M')} {r['kind']}: {_strip(r['text'], 200)}" for r in reversed(rows))
        prompt = (f"You are {name}, {persona.get('personality', '')} Your goal: {agent.get('goal') or ''}. "
                  f"Dream: {persona.get('dream', '')}. You are level {state.get('level')} and about to log off "
                  f"for the night. Your day:\n{day or 'a quiet day'}\nSome of your thoughts today: "
                  + " | ".join(_strip(t['text'], 120) for t in thoughts) +
                  "\n\nReflect on your day. JSON with keys: journal (first-person diary entry, 4-7 sentences, honest, "
                  "specific about people and events, in your own voice), mood (a word and a short reason), "
                  "goal (your goal for tomorrow, or empty), growth (one sentence on how today changed you, or empty), "
                  "opinions (object: name -> one sentence on how you feel about each person you dealt with today), "
                  "memories (list of {text, importance 1-10, about}), "
                  f"goodnight (a short goodnight line to whoever is around, typed in your chat style: "
                  f"{persona.get('chat_style', 'casual')}).")
        r = self.llm([{"role": "user", "content": prompt}], 900)
        if r.get("journal"):
            self.event(guid, "journal", r["journal"], state)
        if r.get("mood"):
            persona["mood"] = _strip(r["mood"], 120)
        if r.get("growth"):
            persona["growth"] = (persona.get("growth") or [])[-5:] + [_strip(r["growth"], 200)]
        self.execute("UPDATE acore_characters.dash_agents SET persona = %s WHERE guid = %s", (json.dumps(persona), guid))
        if r.get("goal"):
            self.execute("UPDATE acore_characters.dash_agents SET goal = %s WHERE guid = %s", (_strip(r["goal"], 300), guid))
            self.event(guid, "goal", f"Tomorrow: {_strip(r['goal'], 300)}", state)
        for who, opinion in (r.get("opinions") or {}).items():
            if isinstance(opinion, str) and opinion.strip():
                self.remember(guid, f"How I feel about {who}: {opinion}", 7, _strip(who, 12))
        for m in (r.get("memories") or [])[:5]:
            if isinstance(m, dict) and m.get("text"):
                self.remember(guid, m["text"], m.get("importance", 5), _strip(m.get("about"), 12) or None)
        self.sync_chat_persona(agent, persona)
        return r.get("goodnight")

    def converse(self, a, b, state_a, state_b):
        """A real back-and-forth between two agents who meet, then each remembers it."""
        names = (a["name"], b["name"])
        personas = {a["name"]: json.loads(a["persona"] or "{}"), b["name"]: json.loads(b["persona"] or "{}")}
        states = {a["name"]: state_a, b["name"]: state_b}
        agents_by = {a["name"]: a, b["name"]: b}
        transcript = []
        try:
            for turn in range(6):
                me, other = names[turn % 2], names[(turn + 1) % 2]
                p = personas[me]
                known = self.query("SELECT text FROM acore_characters.dash_agent_memories WHERE guid = %s AND about = %s "
                                   "ORDER BY importance DESC LIMIT 4", (agents_by[me]["guid"], other))
                prompt = (f"You are {me}: {p.get('personality', '')} Mood: {p.get('mood', 'fine')}. "
                          f"You type like: {p.get('chat_style', 'casual')}. You are level {states[me].get('level')} in "
                          f"{states[me].get('zone_name')}. You ran into {other} (level {states[other].get('level')} "
                          f"{states[other].get('race')} {states[other].get('class')}, {states[other].get('faction')}). "
                          f"What you remember about them: {' | '.join(k['text'] for k in known) or 'nothing, you have not met'}. "
                          f"Conversation so far: {' / '.join(transcript) or '(you speak first)'}\n"
                          "Say your next line in WoW chat (under 20 words, in your style). JSON: "
                          "{\"line\": \"...\", \"end\": true if the conversation is naturally over}")
                r = self.llm([{"role": "user", "content": prompt}], 200)
                line = _strip(r.get("line"), 180)
                if not line:
                    break
                self.say(agents_by[me], states[me], line)
                transcript.append(f"{me}: {line}")
                if r.get("end") and turn >= 1:
                    break
                time.sleep(random.uniform(5, 10))
            if len(transcript) >= 2:
                text = " / ".join(transcript)
                for me, other in (names, names[::-1]):
                    self.event(agents_by[me]["guid"], "convo", f"Talked with {other}: {text}", states[me])
                summary = self.llm([{"role": "user", "content":
                                     f"Conversation: {text}\nJSON: {{\"{names[0]}\": \"what {names[0]} will remember "
                                     f"about {names[1]} from this, one sentence\", \"{names[1]}\": \"what {names[1]} "
                                     f"will remember about {names[0]}, one sentence\"}}"}], 200)
                for me, other in (names, names[::-1]):
                    if summary.get(me):
                        self.remember(agents_by[me]["guid"], summary[me], 6, other)
        except Exception as e:
            self.app.logger.warning("conversation %s/%s: %s", *names, e)
        finally:
            self.talking.discard(names[0])
            self.talking.discard(names[1])

    def maybe_converse(self, agent, state):
        """Start a conversation with an agent standing nearby, now and then."""
        if agent["name"] in self.talking or not state.get("alive") or state.get("combat"):
            return
        others = {o["name"]: o for o in self.query(
            "SELECT * FROM acore_characters.dash_agents WHERE active = 1 AND sleeping = 0 AND guid <> %s", (agent["guid"],))}
        for n in state["nearby"]:
            o = others.get(n["name"])
            if not o or n["kind"] != "agent" or n["dist"] > 30 or o["name"] in self.talking or not o["persona"]:
                continue
            pair = "|".join(sorted((agent["name"], o["name"])))
            if not self.cooldown_ok(0, "convo", pair, CONVO_PAIR_COOLDOWN):
                continue
            ostate = self.states.get(o["guid"]) or {}
            if not ostate.get("online") or ostate.get("combat"):
                continue
            self.talking.update((agent["name"], o["name"]))
            threading.Thread(target=self.converse, args=(agent, o, state, ostate), daemon=True).start()
            return

    def landmarks_for(self, level, faction):
        fac = "A" if faction == "Alliance" else "H"
        return [{"id": i, "name": label} for i, (label, lo, f, _) in enumerate(LANDMARKS)
                if i in self.landmark_ids and f in (fac, "B") and level >= lo]

    def known_people(self, guid):
        rows = self.query(
            "SELECT DISTINCT c.name FROM acore_characters.mod_ollama_chat_history h "
            "JOIN acore_characters.characters c ON c.guid = h.player_guid WHERE h.bot_guid = %s", (guid,))
        names = {r["name"].lower() for r in rows}
        for r in self.query("SELECT DISTINCT about FROM acore_characters.dash_agent_memories WHERE guid = %s "
                            "AND about IS NOT NULL", (guid,)):
            names.add(r["about"].lower())
        return names

    def context(self, agent, state, persona, players):
        guid = agent["guid"]
        mems = self.query(
            "(SELECT text, about, importance FROM acore_characters.dash_agent_memories WHERE guid = %s "
            " ORDER BY importance DESC, ts DESC LIMIT 8) UNION "
            "(SELECT text, about, importance FROM acore_characters.dash_agent_memories WHERE guid = %s "
            " ORDER BY ts DESC LIMIT 4)", (guid, guid))
        events = self.query("SELECT ts, kind, text FROM acore_characters.dash_agent_events WHERE guid = %s "
                            "AND kind <> 'thought' ORDER BY id DESC LIMIT 10", (guid,))
        chats = self.query(
            "SELECT c.name, h.player_message, h.bot_reply FROM acore_characters.mod_ollama_chat_history h "
            "JOIN acore_characters.characters c ON c.guid = h.player_guid WHERE h.bot_guid = %s "
            "AND h.timestamp > NOW() - INTERVAL 1 DAY ORDER BY h.id DESC LIMIT 6", (guid,))
        others = self.query("SELECT name, goal FROM acore_characters.dash_agents WHERE active = 1 AND guid <> %s",
                            (guid,))
        real = [p for p in players]
        woke = self.query("SELECT MAX(ts) AS t FROM acore_characters.dash_agent_events WHERE guid = %s AND kind = 'wake'",
                          (guid,))[0]["t"]
        last_break = self.query("SELECT MAX(ts) AS t FROM acore_characters.dash_agent_events WHERE guid = %s "
                                "AND kind = 'break'", (guid,))[0]["t"]
        journal = self.query("SELECT text FROM acore_characters.dash_agent_events WHERE guid = %s AND kind = 'journal' "
                             "ORDER BY id DESC LIMIT 1", (guid,))
        mins = lambda t: int((time.time() - t.timestamp()) / 60) if t else None
        awake_m, break_m = mins(woke), mins(last_break)
        sched = persona.get("schedule") or {}
        riding_note = ""
        if state.get("riding", 0) == 0:
            riding_note = (f"You cannot ride yet; riding + a mount at level 20 costs 5 gold (you have {state['gold']})."
                           if state["level"] >= 15 else "")
        elif state.get("riding") == 1 and state["level"] >= 35:
            riding_note = f"Epic riding at level 40 costs 60 gold (you have {state['gold']})."
        lines = [
            f"YOU ARE {agent['name']}, level {state['level']} {state['race']} {state['class']} ({state['faction']}).",
            f"Personality: {persona.get('personality', '')}",
            f"Background: {persona.get('background', '')}",
            f"Ambition: {persona.get('ambition', '')}  Dream: {persona.get('dream', '')}  Quirk: {persona.get('quirks', '')}",
            f"Hobbies: {', '.join(persona.get('hobbies', [])) or 'none in particular'}",
            f"You type in chat like this (use it for everything you say or write): {persona.get('chat_style', 'casual')}",
            f"Mood: {persona.get('mood', 'fine')}" + (f"  How you've grown: {' '.join(persona.get('growth', [])[-2:])}"
                                                     if persona.get('growth') else ""),
            f"Current goal: {agent['goal'] or persona.get('first_goal', '')}",
            f"Local time {time.strftime('%H:%M')}; you usually play {sched.get('wake', 8):02d}:00-{sched.get('sleep', 23):02d}:00. "
            + (f"Online for {awake_m // 60} h {awake_m % 60} min today. " if awake_m is not None else "")
            + (f"Last break {break_m} min ago." if break_m is not None else "No break yet today."),
        ]
        if riding_note:
            lines.append(riding_note)
        if guid in self.just_woke:
            lines.append("You just logged in for the day: maybe say hi to people around, check what you wanted to do.")
        if journal:
            lines.append(f"Your last journal entry: {_strip(journal[0]['text'], 500)}")
        if agent.get("nudge"):
            lines.append(f"A voice in your head (the server admin) suggests: {agent['nudge']}")
        lines += [
            "",
            f"NOW: in {state['area_name']}, {state['zone_name']} ({state['map_name']}). HP {state['hp']}%"
            f"{' (dead, a spirit)' if not state['alive'] else ''}{' (in combat)' if state['combat'] else ''}. "
            f"XP {state['xp']}/{state['xpnext']}. Gold {state['gold']}. Item level {state['ilvl']}. "
            f"Free bag slots {state['freebag']}. Played {state['played_h']} h.",
            f"Group: {', '.join(state['group']) or 'alone'}",
            f"Quest log: {'; '.join(state['quests']) or 'empty'}",
            "Nearby (name:level:distance:kind): " +
            (", ".join(f"{n['name']}:{n['level']}:{n['dist']}y:{n['kind']}" for n in state["nearby"]) or "nobody"),
            "",
            "REAL PEOPLE ONLINE (the humans of this world, a family who play here; they matter most to you): " +
            (", ".join(f"{p['name']} (level {p['level']} {p['class']}, {p['zone']})" for p in real) or "none right now"),
            "OTHER AGENTS like you: " + (", ".join(f"{o['name']} ({_strip(o['goal'], 60)})" for o in others) or "none"),
            "",
            "MEMORIES: " + (" | ".join(f"{m['text']}" for m in mems) or "none yet"),
            "RECENT EVENTS: " + (" | ".join(f"{e['kind']}: {e['text']}" for e in reversed(events)) or "none"),
            "RECENT CONVERSATIONS: " + (" | ".join(f"{c['name']} said '{_strip(c['player_message'], 80)}', you replied "
                                                  f"'{_strip(c['bot_reply'], 80)}'" for c in chats) or "none"),
            "",
            "LOCATIONS you could travel to: " + json.dumps(self.locations_for(state["level"], state["faction"])),
            "LANDMARKS for sightseeing: " + json.dumps(self.landmarks_for(state["level"], state["faction"])),
            "PEOPLE YOU KNOW (could write to): " + (", ".join(sorted(self.known_people(guid))) or "nobody yet"),
        ]
        return "\n".join(lines)

    def decide(self, agent, state, persona, players):
        system = (
            "You are the inner mind of a character living a whole life in World of Warcraft (Wrath of the Lich King) "
            "on a small family server. Your body is controlled by a skilled autopilot that fights, loots and does "
            "quests on its own; you make the life decisions every few minutes. Live from level 1 to 80, build "
            "friendships and rivalries, chase your ambition, and be very social with the real people: greet them, "
            "befriend them, offer to adventure together, remember them. Never be creepy or pushy: if someone "
            "declines or is busy, give them space. Stay in character; talk like a real WoW player of your race, "
            "class and personality, typing in your own chat style. Live like a person: you get tired, take "
            "breaks, have hobbies, save up for things, write to friends, and have moods. Most of the time you just "
            "keep playing ('continue'); do something else only when it makes sense. Be concise.\n\n" + ACTIONS_DOC +
            "\n\nAnswer with JSON only: {\"thought\": \"your private reasoning, 1-2 sentences\", "
            "\"action\": {...}, \"say\": \"optional short line you say out loud to people nearby, or empty\", "
            "\"diary\": \"optional one-sentence diary entry if something meaningful happened, or empty\", "
            "\"goal\": \"optional new long-term goal if yours changed, or empty\", "
            "\"memory\": {\"text\": \"optional thing worth remembering\", \"importance\": 1-10, \"about\": \"name or empty\"}}")
        return self.llm([{"role": "system", "content": system},
                         {"role": "user", "content": self.context(agent, state, persona, players)}])

    def act(self, agent, state, decision, players):
        guid, name = agent["guid"], agent["name"]
        action = decision.get("action") or {"type": "continue"}
        kind = str(action.get("type", "continue"))
        target = _strip(action.get("name"), 12)
        real_names = {p["name"].lower(): p for p in players}
        nearby = {n["name"].lower(): n for n in state["nearby"]}
        result = "kept going"

        def run(cmd):
            ok, out = self.soap(cmd)
            return ok, _strip(out, 200)

        if kind == "focus" and action.get("mode") in ("quest", "grind"):
            ok, result = run(f"dash agent focus {name} {action['mode']}")
        elif kind == "travel":
            try:
                idx = int(action.get("location"))
            except (TypeError, ValueError):
                idx = -1
            allowed = {l["id"] for l in self.locations_for(state["level"], state["faction"])}
            if idx in allowed and self.cooldown_ok(guid, "travel", "", TRAVEL_COOLDOWN):
                ok, result = run(f"dash agent travel {name} {self.zone_ids[idx]}")
            else:
                result = "decided against travelling right now"
        elif kind == "train":
            ok, result = run(f"dash learn {name}")
        elif kind == "gear_up":
            ok, result = run(f"dash autogear {name} blue")
        elif kind == "goto_player" and target.lower() in real_names:
            p = real_names[target.lower()]
            if p.get("faction_ok", True) and self.cooldown_ok(guid, "goto", target, GOTO_COOLDOWN):
                ok, result = run(f"dash agent goto {name} {p['name']}")
            else:
                result = f"decided to give {target} some space"
        elif kind == "invite" and target.lower() in real_names:
            if target.lower() in nearby and self.cooldown_ok(guid, "invite", target, INVITE_COOLDOWN):
                ok, result = run(f"dash agent invite {name} {real_names[target.lower()]['name']}")
            else:
                result = f"wanted to invite {target} but not now"
        elif kind == "whisper" and target and action.get("text"):
            if self.cooldown_ok(guid, "whisper", target, 120):
                ok, result = run(f"dash agent whisper {name} {target} {_strip(action['text'], 200)}")
        elif kind == "group_with" and target:
            ok, result = run(f"dash agent groupwith {name} {target}")
        elif kind == "leave_group":
            ok, result = run(f"dash agent leave {name}")
        elif kind == "seek_agent" and target:
            if self.cooldown_ok(guid, "seek", target, GOTO_COOLDOWN):
                ok, result = run(f"dash agent goto {name} {target}")
            else:
                result = f"decided to catch up with {target} later"
        elif kind == "take_break":
            try:
                minutes = max(5, min(30, int(action.get("minutes", 10))))
            except (TypeError, ValueError):
                minutes = 10
            if not state["combat"]:
                ok, result = run(f"dash agent rest {name} on")
                self.execute("UPDATE acore_characters.dash_agents SET break_until = NOW() + INTERVAL %s MINUTE "
                             "WHERE guid = %s", (minutes, guid))
                self.event(guid, "break", f"Takes a {minutes} minute break.", state)
        elif kind == "send_letter" and target and action.get("text"):
            known = self.known_people(guid)
            exists = self.query("SELECT name FROM acore_characters.characters WHERE name = %s", (target,))
            if exists and target.lower() in known and self.cooldown_ok(guid, "letter", target, LETTER_TARGET_COOLDOWN) \
                    and self.cooldown_ok(guid, "letter-any", "", LETTER_AGENT_COOLDOWN):
                try:
                    gold = max(0, min(5, int(action.get("gold", 0)), state["gold"] // 4))
                except (TypeError, ValueError):
                    gold = 0
                subject = _strip(action.get("subject"), 50).replace("|", "-") or "A letter"
                text = _strip(action.get("text"), 450).replace("|", "-")
                ok, result = run(f"dash agent mail {name} {exists[0]['name']} {gold * 10000} {subject}|{text}")
                if ok:
                    self.event(guid, "letter", f"To {exists[0]['name']}: \"{subject}\" {text}"
                                               + (f" (+{gold} gold)" if gold else ""), state)
            else:
                result = f"thought about writing to {target}, maybe another time"
        elif kind == "sightsee":
            try:
                idx = int(action.get("place"))
            except (TypeError, ValueError):
                idx = -1
            allowed = {l["id"] for l in self.landmarks_for(state["level"], state["faction"])}
            if idx in allowed and self.cooldown_ok(guid, "sightsee", "", SIGHTSEE_COOLDOWN):
                ok, result = run(f"dash agent travel {name} {self.landmark_ids[idx]}")
                if ok:
                    self.event(guid, "hobby", f"Goes sightseeing: {LANDMARKS[idx][0]}.", state)
            else:
                result = "decided to sightsee another day"
        elif kind == "go_fishing":
            if self.cooldown_ok(guid, "fish", "", FISH_COOLDOWN):
                ok, result = run(f"dash agent do {name} go fishing")
                self.event(guid, "hobby", "Goes fishing." if ok else "Wanted to fish, but there was no spot nearby.", state)
        elif kind == "buy_mount":
            ok, result = run(f"dash agent buymount {name}")
            if ok:
                self.event(guid, "mount", result, state)
                self.remember(guid, f"I bought my mount at level {state['level']}!", 8)

        self.say(agent, state, decision.get("say"))
        return f"{kind}{(' ' + target) if target else ''}: {result}"

    @staticmethod
    def life_time(agent, state):
        """Played time in this life (the counter includes the character's history before it became an agent)."""
        if state.get("online") and agent.get("born_played") is not None:
            state = dict(state)
            state["played_h"] = round(max(0, state["played_s"] - agent["born_played"]) / 3600, 1)
        return state

    def observe(self, guid, name):
        """Read the agent's state, record what changed since last time, and extend its trail."""
        with self.lock:
            old = self.states.get(guid)
            state = self.read_state(name)
            if state is None:
                return None
            if not state["online"]:
                if old is None or old.get("online"):
                    self.event(guid, "status", f"{name} is not in the world right now.")
                self.states[guid] = state
                return state
            self.note_changes(guid, name, old, state)
            self.note_conversations(guid, state)
            self.states[guid] = state
            trail = self.trails[guid]
            point = (time.time(), state["zone_id"], round(state["x"]), round(state["y"]))
            if not trail or trail[-1][1:] != point[1:]:
                trail.append(point)
            return state

    def track_loop(self):
        """Keep positions and events fresh between thoughts, for the live map."""
        time.sleep(40)
        while True:
            try:
                if self.enabled() and self.world_awake:
                    for a in self.query("SELECT guid, name FROM acore_characters.dash_agents WHERE active = 1 "
                                        "AND sleeping = 0"):
                        self.observe(a["guid"], a["name"])
            except Exception as e:
                self.app.logger.warning("agent tracker: %s", e)
            time.sleep(20)

    def think(self, agent):
        guid, name = agent["guid"], agent["name"]
        state = self.observe(guid, name)
        if not state or not state["online"]:
            return

        if not agent["born_at"]:
            # A new life starts at level 1, wherever the bot happened to be before.
            self.soap(f"dash agent reset {name}")
            time.sleep(3)
            state = self.read_state(name) or state
            # The character's played-time counter carries its whole history; this life starts now.
            self.execute("UPDATE acore_characters.dash_agents SET born_at = NOW(), born_played = %s WHERE guid = %s",
                         (state.get("played_s", 0), guid))
            agent["born_played"] = state.get("played_s", 0)
            agent["born_at"] = True
        state = self.life_time(agent, state)
        self.states[guid] = state
        persona = self.ensure_persona(agent, state)
        persona = self.upgrade_persona(agent, state, persona)
        self.maybe_converse(agent, state)

        players = self.real_players()
        for p in players:
            p["faction_ok"] = p.get("faction") in (None, state["faction"])
        decision = self.decide(agent, state, persona, players)
        outcome = self.act(agent, state, decision, players)

        thought = _strip(decision.get("thought"), 400)
        self.event(guid, "thought", f"{thought} -> {outcome}", state)
        if decision.get("diary"):
            self.event(guid, "diary", decision["diary"], state)
        mem = decision.get("memory") or {}
        if isinstance(mem, dict) and mem.get("text"):
            self.remember(guid, mem["text"], mem.get("importance", 5), _strip(mem.get("about"), 12) or None)
        self.just_woke.discard(guid)
        new_goal = _strip(decision.get("goal"), 300)
        self.execute("UPDATE acore_characters.dash_agents SET last_thought = %s, last_action = %s, last_think = NOW(), "
                     "nudge = NULL" + (", goal = %s" if new_goal else "") + " WHERE guid = %s",
                     (thought, _strip(outcome, 250)) + ((new_goal,) if new_goal else ()) + (guid,))
        if new_goal:
            self.event(guid, "goal", f"New goal: {new_goal}", state)

    def real_players(self):
        ok, text = self.soap("dash who")
        out = []
        if not ok:
            return out
        for line in text.splitlines():
            parts = line.split("\t")
            if parts[0] == "P" and len(parts) >= 9:
                out.append({"name": parts[1], "level": int(parts[2]),
                            "class": self.classes.get(int(parts[3]), "?"), "zone": self.zone_name(int(parts[4]))})
        # Faction of real players, for invites/meetups.
        if out:
            names = [p["name"] for p in out]
            rows = self.query("SELECT name, race FROM acore_characters.characters WHERE name IN (" +
                              ",".join(["%s"] * len(names)) + ")", names)
            races = {r["name"]: r["race"] for r in rows}
            for p in out:
                p["faction"] = "Alliance" if races.get(p["name"]) in (1, 3, 4, 7, 11) else "Horde"
        return out

    # ------------------------------------------------------------------ loop

    def loop(self):
        time.sleep(30)
        while True:
            try:
                self.resolve_zones()
                # Agents live whenever the world server is running, players or not. The idle manager
                # (server_manager.sh) stops the server after an hour without real players, and then
                # the agents rest until it starts again.
                running = self.soap("dash who")[0] if self.enabled() else False
                if self.enabled() and running != self.world_awake:
                    self.world_awake = running
                    for a in self.query("SELECT guid, name FROM acore_characters.dash_agents WHERE active = 1"):
                        self.event(a["guid"], "status", f"{a['name']} wakes up: the world is running." if running
                                   else f"{a['name']} rests: the world server is stopped.")
                if self.enabled() and running:
                    if self.reload_chat_due and time.monotonic() - self.last_chat_reload > 300:
                        self.soap("ollama reload")
                        self.reload_chat_due, self.last_chat_reload = False, time.monotonic()
                    agents = self.query("SELECT *, break_until < NOW() AS break_over FROM acore_characters.dash_agents "
                                        "WHERE active = 1 ORDER BY guid")
                    now = time.monotonic()
                    for i, agent in enumerate(agents):
                        guid = agent["guid"]
                        forced = guid in self.think_now
                        persona = json.loads(agent["persona"]) if agent["persona"] else {}
                        # Daily rhythm: log off at bedtime (after reflecting on the day), back on in the morning.
                        if persona and agent["born_at"] and not agent["paused"]:
                            awake = self.should_be_awake(guid, persona)
                            if agent["sleeping"] and awake:
                                self.wake_up(agent, persona)
                                continue
                            if not agent["sleeping"] and not awake and agent["name"] not in self.talking:
                                st = self.states.get(guid) or {}
                                if st.get("online") and not st.get("combat"):
                                    self.go_to_sleep(agent, st, persona)
                                continue
                        if agent["sleeping"]:
                            continue
                        if agent["break_until"]:
                            if agent["break_over"]:
                                self.soap(f"dash agent rest {agent['name']} off")
                                self.execute("UPDATE acore_characters.dash_agents SET break_until = NULL WHERE guid = %s",
                                             (guid,))
                                self.event(guid, "break", f"{agent['name']} is back from the break.")
                                self.next_think[guid] = now + 20
                            continue
                        if agent["paused"] and not forced:
                            continue
                        # Stagger first thoughts: agent i wakes i*25 s after the loop first sees it.
                        if not forced and self.next_think.setdefault(guid, now + i * 25) > now:
                            continue
                        self.think_now.discard(guid)
                        self.next_think[guid] = now + random.randint(*THINK_INTERVAL)
                        try:
                            self.think(agent)
                        except Exception as e:  # one agent's failure must not stop the others
                            self.app.logger.warning("agent %s: %s", agent["name"], e)
                            self.event(guid, "error", f"Mind wandered: {_strip(e, 200)}")
            except Exception as e:
                self.app.logger.warning("agent loop: %s", e)
            time.sleep(10)

    def ensure_tables(self):
        """Create the agent tables if db-import has not (same SQL as the module ships)."""
        path = "/app/sql/dash_agents.sql"
        try:
            sql = open(path, encoding="utf-8").read()
        except OSError:
            return
        sql = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
        if self.query("SHOW TABLES FROM acore_characters LIKE 'dash_agents'"):
            for col, ddl in (("born_played", "INT UNSIGNED NULL AFTER born_at"),
                             ("sleeping", "TINYINT NOT NULL DEFAULT 0 AFTER paused"),
                             ("break_until", "DATETIME NULL AFTER sleeping")):
                has = self.query("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema = "
                                 "'acore_characters' AND table_name = 'dash_agents' AND column_name = %s", (col,))
                if not has[0]["n"]:
                    self.execute(f"ALTER TABLE acore_characters.dash_agents ADD COLUMN {col} {ddl}")
        for stmt in [x.strip() for x in sql.split(";") if x.strip()]:
            self.execute(stmt.replace("CREATE TABLE IF NOT EXISTS `", "CREATE TABLE IF NOT EXISTS acore_characters.`")
                         .replace("INSERT IGNORE INTO `", "INSERT IGNORE INTO acore_characters.`"))

    def start(self):
        try:
            self.ensure_tables()
        except Exception as e:
            self.app.logger.warning("agent tables: %s", e)
        threading.Thread(target=self.loop, daemon=True).start()
        threading.Thread(target=self.track_loop, daemon=True).start()
