import { readArmy, type ArmyUnit, type ArmyUnitNames } from "../lib/army";

// Official unit art from the Supercell Fan Kit, used under the Fan Content
// Policy the footer links to. Each file is named after the unit's saved id,
// "troop:23" as troop-23.webp; a unit without one shows its name instead.
const ART = import.meta.glob<string>("../units/*.webp", {
  eager: true,
  query: "?no-inline",
  import: "default",
});

function Portrait({
  id,
  count,
  names,
  small = false,
}: {
  id: string;
  count?: number;
  names: ArmyUnitNames;
  small?: boolean;
}) {
  const name = names[id]?.name ?? id;
  const label = count === undefined ? name : `${count} × ${name}`;
  const art = ART[`../units/${id.replace(":", "-")}.webp`];
  return (
    <span
      className={`army-unit${small ? " army-unit-small" : ""}${art ? "" : " army-unit-name"}`}
      title={label}
    >
      {art ? (
        <img src={art} alt={label} loading="lazy" />
      ) : (
        <span aria-label={label}>{name}</span>
      )}
      {count !== undefined && count > 1 ? (
        <span className="army-count" aria-hidden="true">
          {count}
        </span>
      ) : null}
    </span>
  );
}

/** A list row's unit art, from its key ("troop:23", "hero:1|equipment:15",
 * "cc:troop:58,troop:1", "troop:58@5"): beside the row's name, so decorative. */
export function UnitArt({ unitKey }: { unitKey: string }) {
  const art = unitKey
    .replace(/@\d+$/, "")
    .replace(/^cc:/, "")
    .split(/[|,]/)
    .flatMap((id) => ART[`../units/${id.replace(":", "-")}.webp`] ?? [])
    .slice(0, 5);
  if (!art.length) return null;
  return (
    <span className="unit-art" aria-hidden="true">
      {art.map((src, index) => (
        <img key={`${src}-${index}`} src={src} alt="" loading="lazy" />
      ))}
    </span>
  );
}

function Units({ units, names }: { units: ArmyUnit[]; names: ArmyUnitNames }) {
  return units.map((unit, index) => (
    <Portrait key={`${unit.id}-${index}`} id={unit.id} count={unit.count} names={names} />
  ));
}

/** The army a battle used, drawn from its Copy army code. */
export function ArmyPortraits({
  id,
  code,
  names,
}: {
  id: string;
  code: string;
  names: ArmyUnitNames;
}) {
  const army = readArmy(code, names);
  if (army === null) return null;
  const castle = [
    ...army.clanCastle.troops,
    ...army.clanCastle.siege,
    ...army.clanCastle.spells,
  ];
  return (
    <div id={id} className="army-portraits" role="group" aria-label="Army">
      <div className="army-row">
        <Units units={[...army.troops, ...army.siege, ...army.spells]} names={names} />
        {castle.length ? (
          <span className="army-castle" title="Clan Castle">
            <span className="army-castle-label" aria-label="Clan Castle">
              CC
            </span>
            <Units units={castle} names={names} />
          </span>
        ) : null}
      </div>
      {army.heroes.length ? (
        <div className="army-row">
          {army.heroes.map((hero) => (
            <span className="army-hero" key={hero.id}>
              <Portrait id={hero.id} names={names} />
              {hero.equipment.map((id) => (
                <Portrait key={id} id={id} names={names} small />
              ))}
              {hero.pet ? <Portrait id={hero.pet} names={names} small /> : null}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
