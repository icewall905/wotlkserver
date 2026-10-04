/*
 * LLM agents for the ac-manager dashboard.
 *
 * An agent is a random bot whose life decisions (where to go, what to focus on, who to
 * talk to or group with) are made by an LLM running in ac-manager. Playerbots still does
 * the moment-to-moment playing. This file provides:
 *   - protection: agents listed in characters.dash_agents are never re-rolled, logged out
 *     or auto-teleported by the random bot manager;
 *   - .dash agent ... console commands to read an agent's state and carry out decisions.
 */

#include "CellImpl.h"
#include "Chat.h"
#include "CommandScript.h"
#include "DatabaseEnv.h"
#include "GameTime.h"
#include "Group.h"
#include "GroupMgr.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotFactory.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "ScriptMgr.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <set>
#include <sstream>
#include <string>
#include <vector>

using namespace Acore::ChatCommands;

namespace
{
    constexpr uint32 PIN_SECONDS = 365 * 24 * 3600;     // "never" for the random bot manager
    constexpr uint32 REFRESH_MS = 5 * 60 * 1000;         // re-pin every few minutes

    std::set<uint32> g_agents;                           // guid counters from characters.dash_agents
    uint32 g_refreshTimer = 0;

    void LoadAgents()
    {
        g_agents.clear();
        if (QueryResult result = CharacterDatabase.Query("SELECT guid FROM dash_agents WHERE active = 1"))
        {
            do
                g_agents.insert(result->Fetch()[0].Get<uint32>());
            while (result->NextRow());
        }
    }

    void Pin(uint32 guid)
    {
        // "add" keeps the bot in the active set (periodic online/offline), "logout" keeps it
        // logged in, "randomize"/"teleport" stop re-rolls and automatic level teleports.
        for (char const* ev : { "add", "logout", "randomize", "teleport" })
            sRandomPlayerbotMgr.SetBotEventValue(guid, ev, 1, PIN_SECONDS);
    }

    void PinAll()
    {
        LoadAgents();
        for (uint32 guid : g_agents)
            Pin(guid);
    }

    bool IsAgent(Player* player)
    {
        return player && g_agents.count(player->GetGUID().GetCounter());
    }

    std::vector<std::string> Split(char const* args)
    {
        std::vector<std::string> out;
        std::istringstream ss(args ? args : "");
        std::string t;
        while (ss >> t)
            out.push_back(t);
        return out;
    }

    std::string Rest(std::vector<std::string> const& a, size_t from)
    {
        std::string s;
        for (size_t i = from; i < a.size(); ++i)
            s += (s.empty() ? "" : " ") + a[i];
        return s;
    }

    Player* Online(ChatHandler* handler, std::string name)
    {
        if (!normalizePlayerName(name))
        {
            handler->SendErrorMessage("Invalid name.");
            return nullptr;
        }
        Player* p = ObjectAccessor::FindPlayerByName(name);
        if (!p)
            handler->SendErrorMessage("{} is not online.", name);
        return p;
    }

    Player* Agent(ChatHandler* handler, std::string const& name)
    {
        Player* p = Online(handler, name);
        if (p && !GET_PLAYERBOT_AI(p))
        {
            handler->SendErrorMessage("{} is not a bot.", p->GetName());
            return nullptr;
        }
        return p;
    }

    bool Busy(ChatHandler* handler, Player* p)
    {
        if (p->IsInCombat() || p->IsBeingTeleported() || p->IsInFlight())
        {
            handler->SendErrorMessage("{} is busy (combat or travelling).", p->GetName());
            return true;
        }
        return false;
    }

    void TeleportNear(Player* who, uint32 mapId, float x, float y, float z, float o)
    {
        if (!who->IsAlive())
            who->ResurrectPlayer(1.0f);
        if (PlayerbotAI* ai = GET_PLAYERBOT_AI(who))
            ai->Reset();
        who->TeleportTo(mapId, x, y, z, o);
    }

    void SendInvite(Player* from, Player* to)
    {
        WorldPacket data(SMSG_GROUP_INVITE, 10);
        data << uint8(1);
        data << from->GetName();
        data << uint32(0);
        data << uint8(0);
        data << uint32(0);
        to->SendDirectMessage(&data);
    }
}

class dashboard_agents_worldscript : public WorldScript
{
public:
    dashboard_agents_worldscript() : WorldScript("dashboard_agents_worldscript", {
        WORLDHOOK_ON_STARTUP, WORLDHOOK_ON_UPDATE
    }) { }

    void OnStartup() override { PinAll(); }

    void OnUpdate(uint32 diff) override
    {
        if (g_refreshTimer <= diff)
        {
            PinAll();
            g_refreshTimer = REFRESH_MS;
        }
        else
            g_refreshTimer -= diff;
    }
};

class dashboard_agents_playerscript : public PlayerScript
{
public:
    dashboard_agents_playerscript() : PlayerScript("dashboard_agents_playerscript", {
        PLAYERHOOK_ON_LOGIN
    }) { }

    // The random bot manager schedules a re-roll a few seconds after a bot logs in; pin first.
    void OnPlayerLogin(Player* player) override
    {
        if (IsAgent(player))
            Pin(player->GetGUID().GetCounter());
    }
};

class dashboard_agents_commandscript : public CommandScript
{
public:
    dashboard_agents_commandscript() : CommandScript("dashboard_agents_commandscript") { }

    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable agentTable =
        {
            { "state",     HandleState,     SEC_ADMINISTRATOR, Console::Yes },
            { "reload",    HandleReload,    SEC_ADMINISTRATOR, Console::Yes },
            { "reset",     HandleReset,     SEC_ADMINISTRATOR, Console::Yes },
            { "say",       HandleSay,       SEC_ADMINISTRATOR, Console::Yes },
            { "whisper",   HandleWhisper,   SEC_ADMINISTRATOR, Console::Yes },
            { "emote",     HandleEmote,     SEC_ADMINISTRATOR, Console::Yes },
            { "travel",    HandleTravel,    SEC_ADMINISTRATOR, Console::Yes },
            { "goto",      HandleGoto,      SEC_ADMINISTRATOR, Console::Yes },
            { "focus",     HandleFocus,     SEC_ADMINISTRATOR, Console::Yes },
            { "invite",    HandleInvite,    SEC_ADMINISTRATOR, Console::Yes },
            { "groupwith", HandleGroupWith, SEC_ADMINISTRATOR, Console::Yes },
            { "leave",     HandleLeave,     SEC_ADMINISTRATOR, Console::Yes },
        };
        static ChatCommandTable dashTable = { { "agent", agentTable }, { "reroll", HandleReroll, SEC_ADMINISTRATOR, Console::Yes } };
        static ChatCommandTable root = { { "dash", dashTable } };
        return root;
    }

    // .dash reroll -- like ".playerbots rndbot init" but leaves LLM agents alone
    static bool HandleReroll(ChatHandler* handler, char const* /*args*/)
    {
        LoadAgents();
        std::vector<Player*> bots;
        for (auto const& [guid, player] : ObjectAccessor::GetPlayers())
        {
            if (player && player->IsInWorld() && !IsAgent(player) && sRandomPlayerbotMgr.IsRandomBot(player))
                bots.push_back(player);
        }
        for (Player* bot : bots)
            sRandomPlayerbotMgr.RandomizeFirst(bot);
        handler->PSendSysMessage("Re-rolled {} random bots ({} agents left alone).", bots.size(), g_agents.size());
        return true;
    }

    // .dash agent reload -- re-read characters.dash_agents and pin everyone
    static bool HandleReload(ChatHandler* handler, char const* /*args*/)
    {
        PinAll();
        handler->PSendSysMessage("{} agent(s) protected.", g_agents.size());
        return true;
    }

    // .dash agent state <name>   key<TAB>value lines
    static bool HandleState(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 1)
            return false;

        std::string name = a[0];
        if (!normalizePlayerName(name))
            return false;
        Player* p = ObjectAccessor::FindPlayerByName(name);
        if (!p)
        {
            handler->PSendSysMessage("online\t0");
            return true;
        }

        auto kv = [&](char const* k, auto const& v) { handler->PSendSysMessage("{}\t{}", k, v); };
        kv("online", 1);
        kv("level", uint32(p->GetLevel()));
        kv("xp", p->GetUInt32Value(PLAYER_XP));
        kv("xpnext", p->GetUInt32Value(PLAYER_NEXT_LEVEL_XP));
        kv("race", uint32(p->getRace()));
        kv("class", uint32(p->getClass()));
        kv("faction", p->GetTeamId() == TEAM_ALLIANCE ? "Alliance" : "Horde");
        kv("map", p->GetMapId());
        kv("zone", p->GetZoneId());
        kv("area", p->GetAreaId());
        kv("pos", Acore::StringFormat("{:.0f},{:.0f},{:.0f}", p->GetPositionX(), p->GetPositionY(), p->GetPositionZ()));
        kv("hp", uint32(p->GetHealthPct()));
        kv("alive", p->IsAlive() ? 1 : 0);
        kv("combat", p->IsInCombat() ? 1 : 0);
        kv("money", p->GetMoney());
        kv("freebag", p->GetFreeInventorySpace());
        kv("ilvl", uint32(p->GetAverageItemLevel()));
        kv("played", p->GetTotalPlayedTime());
        kv("agent", IsAgent(p) ? 1 : 0);

        std::string group;
        if (Group* g = p->GetGroup())
        {
            for (GroupReference* ref = g->GetFirstMember(); ref; ref = ref->next())
            {
                if (Player* m = ref->GetSource(); m && m != p)
                    group += (group.empty() ? "" : ",") + m->GetName() + (GET_PLAYERBOT_AI(m) ? ":bot" : ":player");
            }
        }
        kv("group", group);

        std::string quests;
        uint32 n = 0;
        for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE && n < 10; ++slot)
        {
            uint32 qid = p->GetQuestSlotQuestId(slot);
            if (!qid)
                continue;
            if (Quest const* q = sObjectMgr->GetQuestTemplate(qid))
            {
                char const* st = p->GetQuestStatus(qid) == QUEST_STATUS_COMPLETE ? "done" : "open";
                quests += (quests.empty() ? "" : "|") + q->GetTitle() + " [" + st + "]";
                ++n;
            }
        }
        kv("quests", quests);

        // Players within 80 yards: name:level:distance:player|bot|agent
        std::string nearby;
        uint32 count = 0;
        p->GetMap()->DoForAllPlayers([&](Player* o)
        {
            if (o == p || count >= 12 || !o->IsInWorld() || o->GetDistance(p) > 80.0f)
                return;
            char const* kind = IsAgent(o) ? "agent" : GET_PLAYERBOT_AI(o) ? "bot" : "player";
            nearby += (nearby.empty() ? "" : ",") + o->GetName() + ":" + std::to_string(o->GetLevel()) + ":" +
                      std::to_string(uint32(o->GetDistance(p))) + ":" + kind;
            ++count;
        });
        kv("nearby", nearby);
        return true;
    }

    // .dash agent reset <name> -- back to level 1 at the race's starting area
    static bool HandleReset(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 1)
            return false;
        Player* p = Agent(handler, a[0]);
        if (!p)
            return false;

        if (p->GetGroup())
            p->RemoveFromGroup();
        p->GiveLevel(1);
        p->SetUInt32Value(PLAYER_XP, 0);
        PlayerbotFactory factory(p, 1, ITEM_QUALITY_NORMAL);
        factory.Randomize(false);

        if (PlayerInfo const* info = sObjectMgr->GetPlayerInfo(p->getRace(), p->getClass()))
            TeleportNear(p, info->mapId, info->positionX, info->positionY, info->positionZ, info->orientation);
        p->SaveToDB(false, false);
        handler->PSendSysMessage("{} starts a new life at level 1.", p->GetName());
        return true;
    }

    // .dash agent say <name> <text>
    static bool HandleSay(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() < 2)
            return false;
        Player* p = Agent(handler, a[0]);
        if (!p)
            return false;
        p->Say(Rest(a, 1), LANG_UNIVERSAL);
        handler->PSendSysMessage("ok");
        return true;
    }

    // .dash agent whisper <name> <target> <text>
    static bool HandleWhisper(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() < 3)
            return false;
        Player* p = Agent(handler, a[0]);
        Player* t = p ? Online(handler, a[1]) : nullptr;
        if (!t)
            return false;
        p->Whisper(Rest(a, 2), LANG_UNIVERSAL, t);
        handler->PSendSysMessage("ok");
        return true;
    }

    // .dash agent emote <name> <textEmoteId>
    static bool HandleEmote(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 2)
            return false;
        Player* p = Agent(handler, a[0]);
        if (!p)
            return false;
        p->HandleEmoteCommand(static_cast<uint32>(std::stoul(a[1])));
        handler->PSendSysMessage("ok");
        return true;
    }

    // .dash agent travel <name> <game_tele id>
    static bool HandleTravel(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 2)
            return false;
        Player* p = Agent(handler, a[0]);
        if (!p || Busy(handler, p))
            return false;
        GameTele const* tele = sObjectMgr->GetGameTele(static_cast<uint32>(std::stoul(a[1])));
        if (!tele)
        {
            handler->SendErrorMessage("Unknown location.");
            return false;
        }
        if (p->GetGroup())
            p->RemoveFromGroup();
        TeleportNear(p, tele->mapId, tele->position_x, tele->position_y, tele->position_z, tele->orientation);
        handler->PSendSysMessage("{} travels to {}.", p->GetName(), tele->name);
        return true;
    }

    // .dash agent goto <name> <player> -- join someone where they are
    static bool HandleGoto(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 2)
            return false;
        Player* p = Agent(handler, a[0]);
        Player* t = p ? Online(handler, a[1]) : nullptr;
        if (!t || Busy(handler, p))
            return false;
        if (t->GetMap()->Instanceable())
        {
            handler->SendErrorMessage("{} is inside an instance.", t->GetName());
            return false;
        }
        float x, y, z;
        t->GetClosePoint(x, y, z, p->GetCombatReach(), 4.0f);
        TeleportNear(p, t->GetMapId(), x, y, z, p->GetAngle(t));
        handler->PSendSysMessage("{} heads over to {}.", p->GetName(), t->GetName());
        return true;
    }

    // .dash agent focus <name> quest|grind
    static bool HandleFocus(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 2 || (a[1] != "quest" && a[1] != "grind"))
            return false;
        Player* p = Agent(handler, a[0]);
        if (!p)
            return false;
        PlayerbotAI* ai = GET_PLAYERBOT_AI(p);
        ai->ChangeStrategy(a[1] == "quest" ? "+new rpg,-grind" : "+grind,-new rpg", BOT_STATE_NON_COMBAT);
        handler->PSendSysMessage("{} focuses on {}.", p->GetName(), a[1] == "quest" ? "questing" : "grinding");
        return true;
    }

    // .dash agent invite <name> <player> -- a real party invite the player can accept or decline
    static bool HandleInvite(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 2)
            return false;
        Player* p = Agent(handler, a[0]);
        Player* t = p ? Online(handler, a[1]) : nullptr;
        if (!t)
            return false;
        if (t->GetGroup() || t->GetGroupInvite())
        {
            handler->SendErrorMessage("{} is already in a group or has an invite pending.", t->GetName());
            return false;
        }
        if (t->GetTeamId() != p->GetTeamId())
        {
            handler->SendErrorMessage("{} is on the other faction.", t->GetName());
            return false;
        }

        Group* group = p->GetGroup();
        if (group && group->GetLeaderGUID() != p->GetGUID())
        {
            handler->SendErrorMessage("{} is not leading their group.", p->GetName());
            return false;
        }
        if (group && group->IsFull())
        {
            handler->SendErrorMessage("{}'s group is full.", p->GetName());
            return false;
        }
        if (!group)
        {
            group = new Group();
            if (!group->AddLeaderInvite(p) || !group->AddInvite(t))
            {
                delete group;
                handler->SendErrorMessage("Could not create the invite.");
                return false;
            }
        }
        else if (!group->AddInvite(t))
        {
            handler->SendErrorMessage("Could not create the invite.");
            return false;
        }
        SendInvite(p, t);
        handler->PSendSysMessage("{} invited {} to a party.", p->GetName(), t->GetName());
        return true;
    }

    // .dash agent groupwith <name> <other bot>
    static bool HandleGroupWith(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 2)
            return false;
        Player* p = Agent(handler, a[0]);
        Player* o = p ? Agent(handler, a[1]) : nullptr;
        if (!o)
            return false;
        if (o->GetTeamId() != p->GetTeamId())
        {
            handler->SendErrorMessage("Different factions.");
            return false;
        }
        if (p->GetGroup() && p->GetGroup() == o->GetGroup())
        {
            handler->PSendSysMessage("Already grouped.");
            return true;
        }

        Group* group = p->GetGroup();
        if (!group)
        {
            group = new Group();
            if (!group->Create(p))
            {
                delete group;
                handler->SendErrorMessage("Could not create a group.");
                return false;
            }
            sGroupMgr->AddGroup(group);
        }
        if (group->IsFull())
        {
            handler->SendErrorMessage("Group is full.");
            return false;
        }
        if (o->GetGroup())
            o->RemoveFromGroup();
        if (!group->AddMember(o))
        {
            handler->SendErrorMessage("Could not add {}.", o->GetName());
            return false;
        }

        // Travel together: bring the joiner over if they are far away and free.
        if (o->GetDistance(p) > 100.0f && !o->IsInCombat() && !p->GetMap()->Instanceable())
        {
            float x, y, z;
            p->GetClosePoint(x, y, z, o->GetCombatReach(), 3.0f);
            TeleportNear(o, p->GetMapId(), x, y, z, o->GetAngle(p));
        }
        handler->PSendSysMessage("{} and {} team up.", p->GetName(), o->GetName());
        return true;
    }

    // .dash agent leave <name>
    static bool HandleLeave(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = Split(args);
        if (a.size() != 1)
            return false;
        Player* p = Agent(handler, a[0]);
        if (!p)
            return false;
        if (!p->GetGroup())
        {
            handler->PSendSysMessage("{} is not in a group.", p->GetName());
            return true;
        }
        p->RemoveFromGroup();
        if (PlayerbotAI* ai = GET_PLAYERBOT_AI(p))
            ai->Reset();
        handler->PSendSysMessage("{} goes their own way.", p->GetName());
        return true;
    }
};

void AddDashboardAgentsScripts()
{
    new dashboard_agents_worldscript();
    new dashboard_agents_playerscript();
    new dashboard_agents_commandscript();
}
