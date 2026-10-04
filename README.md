# wotlkserver: a family AzerothCore server with playerbots, AI chat and LLM agents

> **This is a fork of [AzerothCore](https://github.com/azerothcore/azerothcore-wotlk)**, by way of the
> [mod-playerbots fork](https://github.com/mod-playerbots/azerothcore-wotlk) (its `Playerbot` branch).
> The game server, its database and nearly all of the code in this repository are AzerothCore's work,
> and the bots come from [mod-playerbots](https://github.com/mod-playerbots/mod-playerbots). What this
> fork adds is listed below: a web dashboard, LLM agents, a small server module, scripts and a few
> patches. AzerothCore is licensed under the GNU GPL v2 (see `LICENSE`), and so is this fork.
> Upstream changes are merged from the Playerbot branch.

This repository is the setup for a small private server: about 1000 playerbots, AI chat through an
OpenAI-compatible LLM endpoint, five **LLM agents** that live whole lives in the game, and a web
dashboard to run all of it.

## What is here

| Path | What |
|---|---|
| `manager/` | **ac-manager**, the web dashboard (Flask): overview, agents, characters, items, bots, accounts, GM console, logs, backups |
| `manager/agents.py` | The minds of the LLM agents |
| `modules/mod-dashboard-tools/` | Server module with the `.dash` console commands the dashboard and agents use |
| `modules.lock`, `patches/` | Every other module, pinned to the commit used here, plus small local patches |
| `scripts/setup-modules.sh` | Clones the modules from `modules.lock` and applies `patches/` |
| `scripts/safe-restart.sh` | Restart without losing progress (warn, save, graceful stop) |
| `scripts/merge-conf.py` | Merge a newer `.conf.dist` into a live config, keeping your values |
| `server_manager.sh`, `ac-manager.service` | Idle manager: stops the world server when nobody plays, starts it on login |
| `acmd.sh` | Send a console command and print the reply (`./acmd.sh "server info"`) |
| `docker-compose.override.example.yml`, `.env.example` | Deployment templates |

## Setup

```bash
git clone git@github.com:icewall905/wotlkserver.git && cd wotlkserver
scripts/setup-modules.sh                      # fetch modules at the pinned commits + patches
cp .env.example .env                          # set passwords, ports, MANAGER_BIND
cp docker-compose.override.example.yml docker-compose.override.yml
docker compose build && docker compose up -d
```

`docker compose up` runs db-import first, which creates the agent tables. The dashboard
container (`ac-manager`) is defined in the override file; set `MANAGER_BIND` to a private
or VPN address. Then create the dashboard's SOAP account (GM level 3) with the names from `.env`:

```
./acmd.sh "account create ACMANAGER <SOAP_PASS>"
./acmd.sh "account set gmlevel ACMANAGER 3 -1"
```

Module configs live in `env/dist/etc/modules/` (not committed). Notable settings used here:
`OllamaChat.Url` points at an OpenAI-compatible `/v1/chat/completions` endpoint (vLLM), and
`OllamaChat.RAGDataPath` must be absolute (`/azerothcore/modules/mod-ollama-chat/data/rag/`).

## The dashboard

Served on `MANAGER_BIND:MANAGER_PORT` with HTTP basic auth. It mounts the Docker socket, so
bind it to a private or VPN address only. Tabs:

- **Overview:** players with their bots, services with CPU and memory, lag, bot spread, LLM health, world and database figures, new errors
- **Agents:** the LLM agents (see below)
- **Characters:** alts as party bots (join, ready up, full maintenance, summon), gold, level, teleport, learn spells, autogear
- **Items:** search by class, slot, quality, stat and level, then give to a character (into bags if online, otherwise by mail)
- **Bots, Players, Accounts, Console, Logs, Backups:** management pages; daily automatic backups are kept for 14 days

## LLM agents

Five characters live a whole life from level 1 to 80. Playerbots plays them (combat, quests,
movement). Every two or three minutes each agent's mind, an LLM call, reads its situation,
memories, recent events and conversations, and picks a high-level action: keep questing,
grind, travel to a zone for its level, train, gear up, meet or invite a real player, whisper,
team up with another agent, or leave a group. It may also say something, write a diary
line, update its long-term goal or store a memory.

Everything is recorded in `dash_agent_events` and `dash_agent_memories` and shown in the
Agents tab: persona, level chart, life story (thoughts, conversations, quests, level-ups,
travel, deaths, parties, gear, gold) and memories. From there you can pause an agent, make it
think now, nudge it with a suggestion, or start a new life.

Agents live whenever the world server runs, with or without players. The idle manager
(`server_manager.sh`, installed as `ac-manager.service`) stops the world server after 60 minutes
without real players and starts it again on the next login; while it is stopped the agents
rest and make no LLM calls. The Agents tab shows whether they are living or resting.

Agents are listed in `characters.dash_agents`. The module stops the random bot manager from
re-rolling, logging out or teleporting them, and `.dash reroll` re-rolls every bot except agents.
The tables are created by the module's SQL (`modules/mod-dashboard-tools/data/sql`) during
db-import, and by the dashboard on startup if they are missing.

To choose agents, pick random-bot characters (not death knights) and register them, then
exclude them from the level-bracket module and restart:

```sql
INSERT INTO acore_characters.dash_agents (guid, name)
SELECT guid, name FROM acore_characters.characters WHERE name IN ('Aliannah', 'Manel');
```

```
BotLevelBrackets.ExcludeNames = Aliannah,Manel        # env/dist/etc/modules/mod_player_bot_level_brackets.conf
```

On their first thought, agents are reset to level 1 at their race's starting area and the LLM
writes their persona.

## Local patches

- `mod-ollama-chat`: OpenAI-compatible endpoints (chosen when the URL contains `/chat/completions`)
- `mod-playerbots`: public wrappers for random bot event timers (used to protect agents)
- `mod-congrats-on-level`: skip bots, so the level-up announcements are for real players only

## Operations

- Always restart with `scripts/safe-restart.sh [warning-seconds]`. With 1000 bots, a shutdown
  takes 30-60 s to save everyone, and the compose file allows 5 minutes for it.
- After adding a module, check its SQL created its tables: modules that keep SQL in
  `data/sql/<db>/base` are not picked up by db-import.
