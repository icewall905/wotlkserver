"""LLM agents: bots whose life decisions are made by a language model.

Playerbots plays the character (combat, questing, movement). Every few minutes each agent's
"mind" looks at its situation, memories and recent events, and decides what to do next from a
small set of high-level actions that the mod-dashboard-tools module carries out in game.
Everything an agent thinks, says and does is written to dash_agent_events so it can be
watched in the dashboard.
"""
import json
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
- {"type":"leave_group"}  go your own way again"""


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
        self.lock = threading.Lock()
        self.zone_ids = {}
        self.last_chat = {}
        self.world_awake = None

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
            "ilvl": num("ilvl"), "played_h": round(num("played") / 3600, 1),
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
        lines = [
            f"YOU ARE {agent['name']}, level {state['level']} {state['race']} {state['class']} ({state['faction']}).",
            f"Personality: {persona.get('personality', '')}",
            f"Background: {persona.get('background', '')}",
            f"Ambition: {persona.get('ambition', '')}  Quirk: {persona.get('quirks', '')}",
            f"Current goal: {agent['goal'] or persona.get('first_goal', '')}",
        ]
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
            "class and personality. Be concise.\n\n" + ACTIONS_DOC +
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

        say = _strip(decision.get("say"), 200)
        if say and state["alive"]:
            self.soap(f"dash agent say {name} {say}")
            self.event(guid, "say", say, state)
        return f"{kind}{(' ' + target) if target else ''}: {result}"

    def think(self, agent):
        guid, name = agent["guid"], agent["name"]
        old = self.states.get(guid)
        state = self.read_state(name)
        if state is None:
            return
        if not state["online"]:
            if old is None or old.get("online"):
                self.event(guid, "status", f"{name} is not in the world right now.")
            self.states[guid] = state
            return
        self.note_changes(guid, name, old, state)
        self.note_conversations(guid, state)
        self.states[guid] = state

        if not agent["born_at"]:
            # A new life starts at level 1, wherever the bot happened to be before.
            self.soap(f"dash agent reset {name}")
            self.execute("UPDATE acore_characters.dash_agents SET born_at = NOW() WHERE guid = %s", (guid,))
            time.sleep(3)
            state = self.read_state(name) or state
            self.states[guid] = state
            agent["born_at"] = True
        persona = self.ensure_persona(agent, state)

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
                # Agents only live while someone is around to share the world with: no real player
                # online means no LLM calls. The idle manager stops the server later anyway.
                anyone = bool(self.real_players()) if self.enabled() else False
                if self.enabled() and anyone != self.world_awake:
                    self.world_awake = anyone
                    for a in self.query("SELECT guid, name FROM acore_characters.dash_agents WHERE active = 1"):
                        self.event(a["guid"], "status", f"{a['name']} wakes up: someone is in the world." if anyone
                                   else f"{a['name']} rests: no players online, thinking paused.")
                if self.enabled() and anyone:
                    agents = self.query("SELECT * FROM acore_characters.dash_agents WHERE active = 1 ORDER BY guid")
                    now = time.monotonic()
                    for i, agent in enumerate(agents):
                        guid = agent["guid"]
                        forced = guid in self.think_now
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
        for stmt in [x.strip() for x in sql.split(";") if x.strip()]:
            self.execute(stmt.replace("CREATE TABLE IF NOT EXISTS `", "CREATE TABLE IF NOT EXISTS acore_characters.`")
                         .replace("INSERT IGNORE INTO `", "INSERT IGNORE INTO acore_characters.`"))

    def start(self):
        try:
            self.ensure_tables()
        except Exception as e:
            self.app.logger.warning("agent tables: %s", e)
        threading.Thread(target=self.loop, daemon=True).start()
