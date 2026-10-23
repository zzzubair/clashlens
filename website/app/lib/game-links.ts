// Supercell's links into Clash of Clans: on a phone with the game they open
// it, elsewhere Supercell's page offers to. Tags keep their "#", encoded as
// %23, as ClashPerk's working profile links send them.
const GAME_LINK = "https://link.clashofclans.com/en";

export function copyArmyLink(armyShareCode: string): string {
  return `${GAME_LINK}?action=CopyArmy&army=${encodeURIComponent(armyShareCode)}`;
}

export function playerProfileLink(tag: string): string {
  return `${GAME_LINK}?action=OpenPlayerProfile&tag=${encodeURIComponent(tag)}`;
}

export function clanProfileLink(tag: string): string {
  return `${GAME_LINK}?action=OpenClanProfile&tag=${encodeURIComponent(tag)}`;
}
