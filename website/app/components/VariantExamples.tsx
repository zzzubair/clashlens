import { useEffect } from "react";
import { useFetcher } from "react-router";

import type { ArmyVariantExamples } from "../lib/contracts";
import type { VariantExamplesResponse } from "../routes/army-variant-examples";
import { ArmyPortraits } from "./ArmyPortraits";

/**
 * An opened variant row's most-used armies for the Armies page's players,
 * under the row: loaded on opening, from the address the page gives it.
 */
export function VariantExamples({
  id,
  label,
  attacks,
  url,
  columns,
}: {
  id: string;
  label: string;
  attacks: number;
  url: string;
  columns: number;
}) {
  // Loaded again each time the variant opens; closing it drops what loaded.
  const fetcher = useFetcher<VariantExamplesResponse>({ key: url });
  const { load } = fetcher;
  useEffect(() => {
    void load(url);
  }, [load, url]);
  const examples = fetcher.data?.examples;
  return (
    <tr role="row" className="analytics-examples-row">
      <td role="cell" colSpan={columns}>
        <div
          className="analytics-examples"
          aria-live="polite"
          aria-busy={fetcher.state !== "idle"}
        >
          {examples === undefined ? (
            <p className="section-note">Loading armies…</p>
          ) : examples === null ? (
            <p className="section-note">
              These armies could not load. Close and open {label} to try again.
            </p>
          ) : (
            <ExampleArmies id={id} label={label} attacks={attacks} examples={examples} />
          )}
        </div>
      </td>
    </tr>
  );
}

/** What decides the variant, then each group of alike armies with its
 * attacks, share of the variant's attacks and Copy army. */
export function ExampleArmies({
  id,
  label,
  attacks,
  examples,
}: {
  id: string;
  label: string;
  attacks: number;
  examples: ArmyVariantExamples;
}) {
  return (
    <>
      <p className="analytics-examples-rule">{examples.rule}</p>
      {examples.groups.length ? (
        <ol className="analytics-examples-list">
          {examples.groups.map((group, index) => (
            <li key={group.armyShareCode}>
              <p>
                <strong>
                  {group.attacks.toLocaleString()}{" "}
                  {group.attacks === 1 ? "attack" : "attacks"}
                </strong>{" "}
                · {((attacks ? group.attacks / attacks : 0) * 100).toFixed(1)}% of {label}
              </p>
              <ArmyPortraits
                id={`${id}-army-${index + 1}`}
                code={group.armyShareCode}
                names={examples.armyUnits}
              />
              <a
                className="battle-army"
                href={`https://link.clashofclans.com/en?action=CopyArmy&army=${encodeURIComponent(group.armyShareCode)}`}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`Copy ${label} army ${index + 1}, opens in a new tab`}
              >
                Copy army
              </a>
            </li>
          ))}
        </ol>
      ) : (
        <p className="section-note">No armies to show for these players.</p>
      )}
      <p className="section-note">
        Armies with the same troops, spells, heroes and equipment count as one, leaving
        out troops under 5% of the camp space. Copy army copies the most-used army of
        each.
      </p>
    </>
  );
}
