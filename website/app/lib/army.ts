/**
 * A battle's army, read from its Copy army code: "h" heroes, "u" troops and
 * "s" spells, then "i" Clan Castle troops and "d" Clan Castle spells. Each group
 * lists its units by game unit id, not in the order the code has them, so the
 * same army always looks the same. The format is python/src/clashlens/army_decoder.py's;
 * this reader shows no army rather than guess at a code that decoder rejects.
 */

/** A unit id as Python saves it, such as "troop:23", and how many. */
export interface ArmyUnit {
  id: string;
  count: number;
}

export interface ArmyHero {
  id: string;
  pet: string | null;
  equipment: string[];
}

export interface Army {
  heroes: ArmyHero[];
  troops: ArmyUnit[];
  siege: ArmyUnit[];
  spells: ArmyUnit[];
  clanCastle: { troops: ArmyUnit[]; siege: ArmyUnit[]; spells: ArmyUnit[] };
}

/** Python's name for each unit on the page, and whether it is a siege machine. */
export type ArmyUnitNames = Record<string, { name: string; siege: boolean }>;

const SECTION = /([husid])([^husid]*)/g;
const ENTRY = /^(\d{1,7})x(\d{1,7})$/;
const HERO = /^(\d{1,7})(?:m\d{1,7})?(?:p(\d{1,7}))?(?:e(\d{1,7})(?:_(\d{1,7}))?)?$/;

const byId = (a: string, b: string) => Number(a.split(":")[1]) - Number(b.split(":")[1]);

function units(content: string, kind: "troop" | "spell"): ArmyUnit[] | null {
  const result: ArmyUnit[] = [];
  for (const entry of content.split("-")) {
    const match = ENTRY.exec(entry);
    if (!match) return null;
    const count = Number(match[1]);
    if (count < 1 || count > 1000) return null;
    result.push({ id: `${kind}:${Number(match[2])}`, count });
  }
  return result.sort((a, b) => byId(a.id, b.id));
}

export function readArmy(code: string, names: ArmyUnitNames): Army | null {
  if (code.length > 2000) return null;
  const sections = new Map<string, string>();
  let end = 0;
  for (const match of code.matchAll(SECTION)) {
    if (match.index !== end || sections.has(match[1]) || match[2] === "") return null;
    sections.set(match[1], match[2]);
    end = match.index + match[0].length;
  }
  if (end !== code.length || end === 0) return null;

  const heroes: ArmyHero[] = [];
  for (const chip of sections.get("h")?.split("-") ?? []) {
    const match = HERO.exec(chip);
    if (!match) return null;
    heroes.push({
      id: `hero:${Number(match[1])}`,
      pet: match[2] === undefined ? null : `pet:${Number(match[2])}`,
      equipment: [match[3], match[4]]
        .filter((part) => part !== undefined)
        .map((part) => `equipment:${Number(part)}`)
        .sort(byId),
    });
  }
  heroes.sort((a, b) => byId(a.id, b.id));
  const read = (letter: string, kind: "troop" | "spell") =>
    sections.has(letter) ? units(sections.get(letter) as string, kind) : [];
  const home = read("u", "troop");
  const castle = read("i", "troop");
  const spells = read("s", "spell");
  const castleSpells = read("d", "spell");
  if (!home || !castle || !spells || !castleSpells) return null;
  const isSiege = (unit: ArmyUnit) => names[unit.id]?.siege === true;
  return {
    heroes,
    troops: home.filter((unit) => !isSiege(unit)),
    siege: home.filter(isSiege),
    spells,
    clanCastle: {
      troops: castle.filter((unit) => !isSiege(unit)),
      siege: castle.filter(isSiege),
      spells: castleSpells,
    },
  };
}
