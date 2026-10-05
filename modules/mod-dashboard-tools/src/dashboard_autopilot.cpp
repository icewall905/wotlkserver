/*
 * Autopilot for real players: playerbots plays your own character while you step away.
 *
 * Built on the playerbots "SelfBot" (a PlayerbotAI attached to a player whose master is
 * themselves), with three additions:
 *   - the questing/grinding strategies a random bot uses, re-applied if something resets them;
 *   - the account's NOKICK flag while it runs, so the client's AFK logout is refused;
 *   - the AI is removed at logout (stock SelfBot leaves it behind in the AI map).
 *
 *   .autopilot [quest|grind|off]                 in game, for yourself (no argument toggles quest)
 *   .dash autopilot <player> quest|grind|off|status   console / dashboard
 */

#include "Chat.h"
#include "CommandScript.h"
#include "GameTime.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotRepository.h"
#include "Playerbots.h"
#include "ScriptMgr.h"
#include "WorldSession.h"

#include <map>
#include <string>

using namespace Acore::ChatCommands;

namespace
{
    struct AutopilotEntry
    {
        std::string mode;
        time_t since;
    };

    std::map<ObjectGuid, AutopilotEntry> g_autopilot;
    uint32 g_checkTimer = 0;
    constexpr uint32 CHECK_MS = 10 * IN_MILLISECONDS;

    char const* StrategiesFor(std::string const& mode)
    {
        return mode == "quest" ? "-follow,-stay,+grind,+new rpg" : "-follow,-stay,+grind,-new rpg";
    }

    bool NeedsReapply(PlayerbotAI* ai, std::string const& mode)
    {
        if (!ai->HasStrategy("grind", BOT_STATE_NON_COMBAT) || ai->HasStrategy("follow", BOT_STATE_NON_COMBAT))
            return true;
        return (mode == "quest") != ai->HasStrategy("new rpg", BOT_STATE_NON_COMBAT);
    }

    void Apply(PlayerbotAI* ai, std::string const& mode)
    {
        ai->ChangeStrategy(StrategiesFor(mode), BOT_STATE_NON_COMBAT);
        ai->ChangeStrategy("-follow,-stay", BOT_STATE_DEAD);
    }

    // Returns an empty string on success, otherwise why it could not start.
    std::string Start(Player* player, std::string const& mode)
    {
        PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
        if (ai && !IsSelfBot(player))
            return player->GetName() + " is a bot, not a player.";

        if (!ai)
        {
            PlayerbotsMgr::instance().AddPlayerbotData(player, true);
            ai = GET_PLAYERBOT_AI(player);
            if (!ai)
                return "Playerbots is disabled.";
            ai->SetMaster(player);
            PlayerbotRepository::instance().Load(ai);
        }

        Apply(ai, mode);
        if (!player->GetSession()->HasAccountFlag(ACCOUNT_FLAG_NOKICK))
            player->GetSession()->UpdateAccountFlag(ACCOUNT_FLAG_NOKICK);

        auto it = g_autopilot.find(player->GetGUID());
        if (it == g_autopilot.end())
            g_autopilot[player->GetGUID()] = { mode, GameTime::GetGameTime().count() };
        else
            it->second.mode = mode;
        return "";
    }

    void Stop(Player* player)
    {
        g_autopilot.erase(player->GetGUID());
        if (IsSelfBot(player))
        {
            delete GET_PLAYERBOT_AI(player);
            if (player->isTaxiCheater())
                player->SetTaxiCheater(false);
            player->StopMoving();
            player->GetMotionMaster()->Clear();
        }
        if (WorldSession* session = player->GetSession())
            if (session->HasAccountFlag(ACCOUNT_FLAG_NOKICK))
                session->UpdateAccountFlag(ACCOUNT_FLAG_NOKICK, true);
    }

    std::string FirstLine(std::string const& s)
    {
        return s.substr(0, s.find('\n'));
    }

    bool Run(ChatHandler* handler, Player* player, std::string mode)
    {
        if (mode == "status")
        {
            auto it = g_autopilot.find(player->GetGUID());
            PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
            if (it == g_autopilot.end() || !ai)
            {
                handler->PSendSysMessage("autopilot\toff");
                handler->PSendSysMessage("selfbot\t{}", IsSelfBot(player) ? 1 : 0);
                return true;
            }
            handler->PSendSysMessage("autopilot\ton");
            handler->PSendSysMessage("mode\t{}", it->second.mode);
            handler->PSendSysMessage("since\t{}", it->second.since);
            std::string info = ai->rpgInfo.ToString();
            handler->PSendSysMessage("rpg\t{}", FirstLine(info));
            size_t q = info.find("questId: ");
            if (q != std::string::npos)
                handler->PSendSysMessage("quest\t{}", FirstLine(info.substr(q + 9)));
            handler->PSendSysMessage("stats\t{} accepted, {} turned in", ai->rpgStatistic.questAccepted,
                                     ai->rpgStatistic.questRewarded);
            return true;
        }

        if (mode == "off")
        {
            bool wasOn = g_autopilot.count(player->GetGUID()) || IsSelfBot(player);
            Stop(player);
            handler->PSendSysMessage(wasOn ? "Autopilot off: {} is yours again." : "Autopilot was not on for {}.",
                                     player->GetName());
            if (wasOn && handler->GetSession() != player->GetSession())
                ChatHandler(player->GetSession()).SendSysMessage("Autopilot is off. You have control.");
            return true;
        }

        if (mode != "quest" && mode != "grind")
        {
            handler->SendErrorMessage("Mode must be quest, grind, off or status.");
            return false;
        }

        std::string error = Start(player, mode);
        if (!error.empty())
        {
            handler->SendErrorMessage("{}", error);
            return false;
        }

        handler->PSendSysMessage("Autopilot on for {} ({}).", player->GetName(),
                                 mode == "quest" ? "questing and grinding" : "grinding nearby");
        if (handler->GetSession() != player->GetSession())
            ChatHandler(player->GetSession()).SendSysMessage(
                "Autopilot is on. Type .autopilot off (or use the dashboard) to take control back.");
        return true;
    }
}

class dashboard_autopilot_worldscript : public WorldScript
{
public:
    dashboard_autopilot_worldscript() : WorldScript("dashboard_autopilot_worldscript", { WORLDHOOK_ON_UPDATE }) { }

    // Group changes, deaths and "reset" commands rebuild the strategies from the defaults,
    // which for a SelfBot means standing still. Put the autopilot strategies back.
    void OnUpdate(uint32 diff) override
    {
        if (g_checkTimer > diff)
        {
            g_checkTimer -= diff;
            return;
        }
        g_checkTimer = CHECK_MS;

        for (auto it = g_autopilot.begin(); it != g_autopilot.end();)
        {
            Player* player = ObjectAccessor::FindPlayer(it->first);
            PlayerbotAI* ai = player ? GET_PLAYERBOT_AI(player) : nullptr;
            if (!player || !ai || !IsSelfBot(player))
            {
                if (player)
                    Stop(player);
                it = g_autopilot.erase(it);
                continue;
            }
            if (NeedsReapply(ai, it->second.mode))
                Apply(ai, it->second.mode);
            ++it;
        }
    }
};

class dashboard_autopilot_playerscript : public PlayerScript
{
public:
    dashboard_autopilot_playerscript() : PlayerScript("dashboard_autopilot_playerscript", {
        PLAYERHOOK_ON_BEFORE_LOGOUT
    }) { }

    void OnPlayerBeforeLogout(Player* player) override
    {
        if (g_autopilot.count(player->GetGUID()))
            Stop(player);
    }
};

class dashboard_autopilot_commandscript : public CommandScript
{
public:
    dashboard_autopilot_commandscript() : CommandScript("dashboard_autopilot_commandscript") { }

    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable dashTable = { { "autopilot", HandleDashAutopilot, SEC_ADMINISTRATOR, Console::Yes } };
        static ChatCommandTable root =
        {
            { "dash",      dashTable },
            { "autopilot", HandleAutopilot, SEC_PLAYER, Console::No },
        };
        return root;
    }

    static bool HandleAutopilot(ChatHandler* handler, Optional<std::string> mode)
    {
        Player* player = handler->GetSession()->GetPlayer();
        if (!mode)
            return Run(handler, player, g_autopilot.count(player->GetGUID()) ? "off" : "quest");
        return Run(handler, player, *mode);
    }

    static bool HandleDashAutopilot(ChatHandler* handler, std::string name, std::string mode)
    {
        if (!normalizePlayerName(name))
        {
            handler->SendErrorMessage("Invalid character name.");
            return false;
        }
        Player* player = ObjectAccessor::FindPlayerByName(name);
        if (!player)
        {
            handler->SendErrorMessage("{} is not online.", name);
            return false;
        }
        return Run(handler, player, mode);
    }
};

void AddDashboardAutopilotScripts()
{
    new dashboard_autopilot_worldscript();
    new dashboard_autopilot_playerscript();
    new dashboard_autopilot_commandscript();
}
