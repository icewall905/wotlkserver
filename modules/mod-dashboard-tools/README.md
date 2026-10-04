# mod-dashboard-tools

Console/SOAP commands used by the `ac-manager` web dashboard. All require
administrator security and target an online player by name, so they work from
the server console and SOAP where no player session exists.

| Command | Effect |
|---|---|
| `.dash altbot add <master> <alt>` | Log `<alt>` in as a playerbot controlled by `<master>` (joins their party) |
| `.dash altbot remove <master> <alt>` | Log the alt bot out again |
| `.dash altbot invite <master> <alt>` | Put an online alt bot into the master's party (and make the master leader if a bot leads it) |
| `.dash summon <master> <bot\|all>` | Teleport one or all of the master's bots to the master (revives dead ones) |
| `.dash botcmd <master> <bot> <command>` | Send a bot a chat command as if its master whispered it (e.g. `autogear`, `maintenance`) |
| `.dash autogear <player> [green\|blue\|epic\|legendary]` | Upgrade-only autogear for any online character; replaced items go to the bags |
| `.dash learn <player>` | Learn all trainer class spells for the current level, quest-reward spells, and max weapon/defense skills |
| `.dash who` | Tab-separated snapshot of real players (with their bots) and bot totals, for the dashboard |
| `.dash additem <player> <itemId> [count]` | Put items straight into the player's bags |
| `.dash addmoney <player> <copper>` | Add (or remove, if negative) money |
