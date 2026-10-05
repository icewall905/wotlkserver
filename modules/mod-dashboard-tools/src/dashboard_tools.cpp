/*
 * Console-capable helper commands for the ac-manager web dashboard.
 *
 * The stock equivalents (.playerbots bot add, .additem, .modify money) need a
 * player session or a selected target, which SOAP and the server console do
 * not have. These take the target player by name instead.
 */

#include "Chat.h"
#include "Group.h"
#include "GroupMgr.h"
#include "CommandScript.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Bag.h"
#include "Player.h"
#include "Trainer.h"
#include "Playerbots.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "PlayerbotFactory.h"
#include "PlayerbotMgr.h"
#include "RandomPlayerbotMgr.h"
#include "WorldSession.h"

#include <map>
#include <sstream>
#include <string>

using namespace Acore::ChatCommands;

namespace
{
    std::vector<std::string> SplitArgs(char const* args)
    {
        std::vector<std::string> out;
        std::istringstream ss(args ? args : "");
        std::string token;
        while (ss >> token)
            out.push_back(token);
        return out;
    }

    Player* FindOnline(ChatHandler* handler, std::string const& name)
    {
        std::string normalized = name;
        if (!normalizePlayerName(normalized))
        {
            handler->SendErrorMessage("Invalid character name '{}'.", name);
            return nullptr;
        }

        Player* player = ObjectAccessor::FindPlayerByName(normalized);
        if (!player)
            handler->SendErrorMessage("{} is not online.", normalized);
        return player;
    }
}

class dashboard_tools_commandscript : public CommandScript
{
public:
    dashboard_tools_commandscript() : CommandScript("dashboard_tools_commandscript") { }

    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable dashCommandTable =
        {
            { "altbot",   HandleAltBot,   SEC_ADMINISTRATOR, Console::Yes },
            { "botcmd",   HandleBotCmd,   SEC_ADMINISTRATOR, Console::Yes },
            { "autogear", HandleAutoGear, SEC_ADMINISTRATOR, Console::Yes },
            { "summon",   HandleSummon,   SEC_ADMINISTRATOR, Console::Yes },
            { "learn",    HandleLearn,    SEC_ADMINISTRATOR, Console::Yes },
            { "who",      HandleWho,      SEC_ADMINISTRATOR, Console::Yes },
            { "additem",  HandleAddItem,  SEC_ADMINISTRATOR, Console::Yes },
            { "addmoney", HandleAddMoney, SEC_ADMINISTRATOR, Console::Yes },
            { "sell",     HandleSell,     SEC_ADMINISTRATOR, Console::Yes },
        };

        static ChatCommandTable commandTable =
        {
            { "dash", dashCommandTable },
        };

        return commandTable;
    }

    // .dash altbot add|remove <master> <alt>
    static bool HandleAltBot(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if ((a.size() != 3 && !(a.size() == 2 && a[0] == "regroup")) ||
            (a[0] != "add" && a[0] != "remove" && a[0] != "invite" && a[0] != "regroup"))
        {
            handler->SendErrorMessage("Usage: .dash altbot add|remove|invite <master> <alt> | regroup <master>");
            return false;
        }

        Player* master = FindOnline(handler, a[1]);
        if (!master)
            return false;

        if (a[0] == "invite")
            return InviteAltBot(handler, master, a[2]);

        if (a[0] == "regroup")
            return Regroup(handler, master);

        // A bot joining a full party makes playerbots convert it to a raid; refuse instead.
        if (a[0] == "add")
        {
            Group* group = master->GetGroup();
            if (group && !group->isRaidGroup() && group->IsFull())
            {
                handler->SendErrorMessage("{}'s party is full (5/5). Send someone home first.", master->GetName());
                return false;
            }
        }

        PlayerbotMgr* mgr = GET_PLAYERBOT_MGR(master);
        if (!mgr)
        {
            handler->SendErrorMessage("{} cannot control bots yet.", master->GetName());
            return false;
        }

        std::string const cmd = a[0] + " " + a[2];
        for (std::string const& line : mgr->HandlePlayerbotCommand(cmd.c_str(), master))
            handler->PSendSysMessage("{}", line);

        return true;
    }

    // Replace the master's group (typically a raid it was converted into) with a normal party:
    // the master as leader plus up to four of their own bots. Other members are dropped.
    static bool Regroup(ChatHandler* handler, Player* master)
    {
        std::vector<ObjectGuid> keep;
        if (PlayerbotMgr* mgr = GET_PLAYERBOT_MGR(master))
        {
            for (auto it = mgr->GetPlayerBotsBegin(); it != mgr->GetPlayerBotsEnd() && keep.size() < 4; ++it)
            {
                if (it->second)
                    keep.push_back(it->second->GetGUID());
            }
        }

        if (Group* old = master->GetGroup())
            old->Disband();

        if (keep.empty())
        {
            handler->PSendSysMessage("{} has no bots logged in; the old group was disbanded.", master->GetName());
            return true;
        }

        Group* group = new Group;
        if (!group->Create(master))
        {
            delete group;
            handler->SendErrorMessage("Could not create a party for {}.", master->GetName());
            return false;
        }
        sGroupMgr->AddGroup(group);

        std::string names;
        for (ObjectGuid const& guid : keep)
        {
            Player* bot = ObjectAccessor::FindPlayer(guid);
            if (bot && !bot->GetGroup() && group->AddMember(bot))
                names += (names.empty() ? "" : ", ") + bot->GetName();
        }
        handler->PSendSysMessage("{} now leads a normal party with {}.", master->GetName(), names.empty() ? "nobody" : names);
        return true;
    }

    // Put an online alt bot into its master's party. The playerbots login hook queues this
    // too, but it silently gives up in some states, so the dashboard calls this afterwards.
    static bool InviteAltBot(ChatHandler* handler, Player* master, std::string const& altName)
    {
        Player* alt = FindOnline(handler, altName);
        if (!alt)
            return false;

        PlayerbotAI* altAI = GET_PLAYERBOT_AI(alt);
        if (!altAI || altAI->GetMaster() != master)
        {
            handler->SendErrorMessage("{} is not a bot controlled by {}.", alt->GetName(), master->GetName());
            return false;
        }

        Group* group = master->GetGroup();
        if (group && alt->GetGroup() == group)
        {
            handler->PSendSysMessage("{} is already in {}'s party.", alt->GetName(), master->GetName());
        }
        else
        {
            if (alt->GetGroup())
                alt->RemoveFromGroup();

            if (!group)
            {
                group = new Group;
                if (!group->Create(master))
                {
                    delete group;
                    handler->SendErrorMessage("Could not create a party for {}.", master->GetName());
                    return false;
                }
                sGroupMgr->AddGroup(group);
            }

            if (group->IsFull())
            {
                handler->SendErrorMessage("{}'s {} is full. Send someone home first.", master->GetName(),
                                          group->isRaidGroup() ? "raid" : "party (5/5)");
                return false;
            }

            if (!group->AddMember(alt))
            {
                handler->SendErrorMessage("Could not add {} to the party.", alt->GetName());
                return false;
            }
            handler->PSendSysMessage("{} joined {}'s party.", alt->GetName(), master->GetName());
        }

        // A party left over from an earlier session can be led by one of the master's own bots.
        if (group->GetLeaderGUID() != master->GetGUID())
        {
            Player* leader = ObjectAccessor::FindPlayer(group->GetLeaderGUID());
            PlayerbotAI* leaderAI = leader ? GET_PLAYERBOT_AI(leader) : nullptr;
            if (!leader || (leaderAI && leaderAI->GetMaster() == master))
            {
                group->ChangeLeader(master->GetGUID());
                handler->PSendSysMessage("{} is now party leader.", master->GetName());
            }
        }

        return true;
    }

    static void SummonTo(Player* master, Player* target)
    {
        if (target->IsBeingTeleported())
            return;

        if (target->IsInFlight())
        {
            target->GetMotionMaster()->MovementExpired();
            target->CleanupAfterTaxiFlight();
        }

        if (!target->IsAlive())
            target->ResurrectPlayer(1.0f);

        float x, y, z;
        master->GetClosePoint(x, y, z, target->GetCombatReach());
        target->TeleportTo(master->GetMapId(), x, y, z, target->GetAngle(master));
    }

    // .dash summon <master> <bot|all>
    // Brings one of the master's bots, or all of them, to the master.
    static bool HandleSummon(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if (a.size() != 2)
        {
            handler->SendErrorMessage("Usage: .dash summon <master> <bot|all>");
            return false;
        }

        Player* master = FindOnline(handler, a[0]);
        if (!master)
            return false;

        if (master->IsBeingTeleported() || master->IsInFlight())
        {
            handler->SendErrorMessage("{} is travelling; try again when they have arrived.", master->GetName());
            return false;
        }

        if (a[1] == "all")
        {
            PlayerbotMgr* mgr = GET_PLAYERBOT_MGR(master);
            uint32 count = 0;
            if (mgr)
            {
                for (auto it = mgr->GetPlayerBotsBegin(); it != mgr->GetPlayerBotsEnd(); ++it)
                {
                    if (Player* bot = it->second)
                    {
                        SummonTo(master, bot);
                        ++count;
                    }
                }
            }
            handler->PSendSysMessage("Summoned {} bot(s) to {}.", count, master->GetName());
            return true;
        }

        Player* target = FindOnline(handler, a[1]);
        if (!target)
            return false;

        PlayerbotAI* targetAI = GET_PLAYERBOT_AI(target);
        if (!targetAI || targetAI->GetMaster() != master)
        {
            handler->SendErrorMessage("{} is not a bot controlled by {}.", target->GetName(), master->GetName());
            return false;
        }

        SummonTo(master, target);
        handler->PSendSysMessage("Summoned {} to {}.", target->GetName(), master->GetName());
        return true;
    }

    // .dash botcmd <master> <bot> <command text...>
    // Delivers a chat command to a bot as if its master had whispered it
    // (autogear, maintenance, talents, ...).
    static bool HandleBotCmd(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if (a.size() < 3)
        {
            handler->SendErrorMessage("Usage: .dash botcmd <master> <bot> <command>");
            return false;
        }

        Player* master = FindOnline(handler, a[0]);
        Player* bot = master ? FindOnline(handler, a[1]) : nullptr;
        if (!bot)
            return false;

        PlayerbotAI* botAI = GET_PLAYERBOT_AI(bot);
        if (!botAI || botAI->GetMaster() != master)
        {
            handler->SendErrorMessage("{} is not a bot controlled by {}.", bot->GetName(), master->GetName());
            return false;
        }

        std::string text = a[2];
        for (size_t i = 3; i < a.size(); ++i)
            text += " " + a[i];

        botAI->HandleCommand(CHAT_MSG_WHISPER, text, master);
        handler->PSendSysMessage("Sent '{}' to {}.", text, bot->GetName());
        return true;
    }

    // .dash autogear <player> [green|blue|epic|legendary]
    // Upgrade-only gearing for any character, bot or not. Replaced items go to the bags;
    // slots whose old item does not fit in the bags are left alone.
    static bool HandleAutoGear(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if (a.empty() || a.size() > 2)
        {
            handler->SendErrorMessage("Usage: .dash autogear <player> [green|blue|epic|legendary]");
            return false;
        }

        Player* player = FindOnline(handler, a[0]);
        if (!player)
            return false;

        if (player->IsInCombat())
        {
            handler->SendErrorMessage("{} is in combat.", player->GetName());
            return false;
        }

        uint32 quality = static_cast<uint32>(sPlayerbotAIConfig.autoGearQualityLimit);
        if (a.size() == 2)
        {
            if (a[1] == "green")
                quality = ITEM_QUALITY_UNCOMMON;
            else if (a[1] == "blue")
                quality = ITEM_QUALITY_RARE;
            else if (a[1] == "epic")
                quality = ITEM_QUALITY_EPIC;
            else if (a[1] == "legendary")
                quality = ITEM_QUALITY_LEGENDARY;
            else
            {
                handler->SendErrorMessage("Quality must be green, blue, epic or legendary.");
                return false;
            }
        }

        PlayerbotFactory::AutoGear(player, quality, 0, /*incremental*/ true);
        handler->PSendSysMessage("Auto-geared {} (level {}, up to {} quality). Replaced items are in the bags.",
                                 player->GetName(), player->GetLevel(),
                                 quality == ITEM_QUALITY_LEGENDARY ? "legendary" : quality == ITEM_QUALITY_EPIC ? "epic"
                                 : quality == ITEM_QUALITY_RARE ? "blue" : "green");
        return true;
    }

    // .dash who
    // Machine-readable snapshot of who is online, for the dashboard overview. One line each:
    //   P <name> <level> <class> <zoneId> <mapId> <latencyMs> <accountId> <bot1,bot2,...> <autopilot 0|1>
    //                                                                                       real player
    //   T <randomBots> <altBots> <deadBots> <inCombatBots>                                  totals
    //   L <level> <count>                                                                   random bots per level
    //   Z <zoneId> <count>                                                                  random bots per zone
    //   F <alliance> <horde>                                                                random bots per faction
    // Levels and zones are live; the characters table only catches up on the next save.
    static bool HandleWho(ChatHandler* handler, char const* /*args*/)
    {
        uint32 randomBots = 0, altBots = 0, deadBots = 0, combatBots = 0, alliance = 0, horde = 0;
        std::map<uint32, uint32> levels, zones;

        for (auto const& [guid, player] : ObjectAccessor::GetPlayers())
        {
            if (!player || !player->IsInWorld())
                continue;

            // A SelfBot (autopilot) is still a person at a client, so it counts as a player.
            PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
            bool selfBot = ai && IsSelfBot(player);
            if (ai && !selfBot)
            {
                if (sRandomPlayerbotMgr.IsRandomBot(player))
                {
                    ++randomBots;
                    ++levels[player->GetLevel()];
                    ++zones[player->GetZoneId()];
                    if (player->GetTeamId() == TEAM_ALLIANCE)
                        ++alliance;
                    else
                        ++horde;
                }
                else
                    ++altBots;
                if (!player->IsAlive())
                    ++deadBots;
                if (player->IsInCombat())
                    ++combatBots;
                continue;
            }

            std::string bots;
            if (PlayerbotMgr* mgr = GET_PLAYERBOT_MGR(player))
            {
                for (auto it = mgr->GetPlayerBotsBegin(); it != mgr->GetPlayerBotsEnd(); ++it)
                {
                    if (it->second)
                        bots += (bots.empty() ? "" : ",") + it->second->GetName();
                }
            }

            handler->PSendSysMessage("P\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}", player->GetName(), player->GetLevel(),
                                     player->getClass(), player->GetZoneId(), player->GetMapId(),
                                     player->GetSession()->GetLatency(), player->GetSession()->GetAccountId(), bots,
                                     selfBot ? 1 : 0);
        }

        handler->PSendSysMessage("T\t{}\t{}\t{}\t{}", randomBots, altBots, deadBots, combatBots);
        for (auto const& [level, count] : levels)
            handler->PSendSysMessage("L\t{}\t{}", level, count);
        for (auto const& [zone, count] : zones)
            handler->PSendSysMessage("Z\t{}\t{}", zone, count);
        handler->PSendSysMessage("F\t{}\t{}", alliance, horde);
        return true;
    }

    // .dash learn <player>
    // Everything the class trainers would teach at the current level (free), quest-reward
    // class spells, and weapon/defense skills raised to the level cap. Talents are untouched.
    // Same loop as .learn all my trainer, which needs a session.
    static bool HandleLearn(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if (a.size() != 1)
        {
            handler->SendErrorMessage("Usage: .dash learn <player>");
            return false;
        }

        Player* player = FindOnline(handler, a[0]);
        if (!player)
            return false;

        uint32 const before = player->GetSpellMap().size();
        std::vector<Trainer::Trainer const*> const& trainers = sObjectMgr->GetClassTrainers(player->getClass());

        bool hadNew;
        do
        {
            hadNew = false;
            for (Trainer::Trainer const* trainer : trainers)
            {
                if (!trainer->IsTrainerValidForPlayer(player))
                    continue;

                for (Trainer::Spell const& trainerSpell : trainer->GetSpells())
                {
                    if (!trainer->CanTeachSpell(player, &trainerSpell))
                        continue;

                    if (trainerSpell.IsCastable())
                        player->CastSpell(player, trainerSpell.SpellId, true);
                    else
                        player->learnSpell(trainerSpell.SpellId, false);

                    if (!trainer->CanTeachSpell(player, &trainerSpell))
                        hadNew = true;
                }
            }
        } while (hadNew);

        player->learnQuestRewardedSpells();
        player->UpdateSkillsToMaxSkillsForLevel();

        uint32 const learned = player->GetSpellMap().size() - before;
        handler->PSendSysMessage("{} learned {} new spell(s) for level {} and has weapon/defense skills maxed.",
                                 player->GetName(), learned, player->GetLevel());
        return true;
    }

    // .dash additem <player> <itemId> [count]
    static bool HandleAddItem(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if (a.size() < 2 || a.size() > 3)
        {
            handler->SendErrorMessage("Usage: .dash additem <player> <itemId> [count]");
            return false;
        }

        Player* player = FindOnline(handler, a[0]);
        if (!player)
            return false;

        uint32 itemId = 0;
        uint32 count = 1;
        try
        {
            itemId = std::stoul(a[1]);
            if (a.size() == 3)
                count = std::stoul(a[2]);
        }
        catch (std::exception const&)
        {
            handler->SendErrorMessage("Item id and count must be numbers.");
            return false;
        }

        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(itemId);
        if (!proto)
        {
            handler->SendErrorMessage("Item {} does not exist.", itemId);
            return false;
        }

        if (count < 1 || count > 1000)
        {
            handler->SendErrorMessage("Count must be between 1 and 1000.");
            return false;
        }

        uint32 noSpaceForCount = 0;
        ItemPosCountVec dest;
        InventoryResult msg = player->CanStoreNewItem(NULL_BAG, NULL_SLOT, dest, itemId, count, &noSpaceForCount);
        uint32 const storable = msg == EQUIP_ERR_OK ? count : count - noSpaceForCount;

        if (storable == 0 || dest.empty())
        {
            handler->SendErrorMessage("{} has no room for {} (error {}).", player->GetName(), proto->Name1,
                                      static_cast<uint32>(msg));
            return false;
        }

        Item* item = player->StoreNewItem(dest, itemId, true, Item::GenerateItemRandomPropertyId(itemId));
        if (!item)
        {
            handler->SendErrorMessage("Could not create {}.", proto->Name1);
            return false;
        }

        player->SendNewItem(item, storable, true, false);
        handler->PSendSysMessage("Added {}x {} to {}.", storable, proto->Name1, player->GetName());
        if (storable < count)
            handler->PSendSysMessage("{} did not fit in the bags.", count - storable);

        return true;
    }

    // .dash addmoney <player> <copper>
    // Whether a vendor trip would get rid of this item: grey junk, or common/uncommon armour and
    // weapons the character cannot use or that are no better than what it already wears.
    static bool IsVendorTrash(Player* player, Item* item)
    {
        ItemTemplate const* proto = item->GetTemplate();
        if (!proto || !proto->SellPrice || item->IsInTrade())
            return false;

        if (proto->Quality == ITEM_QUALITY_POOR)
            return true;

        if ((proto->Class != ITEM_CLASS_ARMOR && proto->Class != ITEM_CLASS_WEAPON) ||
            proto->Quality > ITEM_QUALITY_UNCOMMON)
            return false;

        if (player->CanUseItem(proto) != EQUIP_ERR_OK)
            return true;

        uint8 slot = player->FindEquipSlot(proto, NULL_SLOT, true);
        if (slot == NULL_SLOT)
            return true;

        Item* equipped = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
        return equipped && equipped->GetTemplate()->ItemLevel >= proto->ItemLevel;
    }

    // .dash sell <player> -- sell vendor trash from the bags, as if visiting a vendor
    static bool HandleSell(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if (a.size() != 1)
        {
            handler->SendErrorMessage("Usage: .dash sell <player>");
            return false;
        }

        Player* player = FindOnline(handler, a[0]);
        if (!player)
            return false;

        if (player->IsInCombat())
        {
            handler->SendErrorMessage("{} is in combat.", player->GetName());
            return false;
        }

        std::vector<std::pair<uint8, uint8>> toSell;
        for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        {
            if (Item* item = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot); item && IsVendorTrash(player, item))
                toSell.emplace_back(INVENTORY_SLOT_BAG_0, slot);
        }

        for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
        {
            Item* bagItem = player->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot);
            Bag* bag = bagItem ? bagItem->ToBag() : nullptr;
            if (!bag)
                continue;

            for (uint32 slot = 0; slot < bag->GetBagSize(); ++slot)
            {
                if (Item* item = bag->GetItemByPos(slot); item && IsVendorTrash(player, item))
                    toSell.emplace_back(bagSlot, slot);
            }
        }

        uint32 copper = 0;
        for (auto const& [bag, slot] : toSell)
        {
            Item* item = player->GetItemByPos(bag, slot);
            copper += item->GetTemplate()->SellPrice * item->GetCount();
            player->DestroyItem(bag, slot, true);
        }

        if (copper)
            player->ModifyMoney(static_cast<int32>(copper));

        handler->PSendSysMessage("{} sold {} item(s) for {}g {}s {}c and has {} free bag slots.", player->GetName(),
                                 toSell.size(), copper / 10000, (copper / 100) % 100, copper % 100,
                                 player->GetFreeInventorySpace());
        return true;
    }

    static bool HandleAddMoney(ChatHandler* handler, char const* args)
    {
        std::vector<std::string> a = SplitArgs(args);
        if (a.size() != 2)
        {
            handler->SendErrorMessage("Usage: .dash addmoney <player> <copper>");
            return false;
        }

        Player* player = FindOnline(handler, a[0]);
        if (!player)
            return false;

        int64 amount = 0;
        try
        {
            amount = std::stoll(a[1]);
        }
        catch (std::exception const&)
        {
            handler->SendErrorMessage("Amount must be a number of copper.");
            return false;
        }

        if (amount > static_cast<int64>(MAX_MONEY_AMOUNT) || amount < -static_cast<int64>(MAX_MONEY_AMOUNT))
        {
            handler->SendErrorMessage("Amount out of range.");
            return false;
        }

        player->ModifyMoney(static_cast<int32>(amount));
        handler->PSendSysMessage("{} now has {}g {}s {}c.", player->GetName(), player->GetMoney() / 10000,
                                 (player->GetMoney() / 100) % 100, player->GetMoney() % 100);
        return true;
    }
};

void AddDashboardToolsScripts()
{
    new dashboard_tools_commandscript();
}
