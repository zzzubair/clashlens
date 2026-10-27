import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useFetcher } from "react-router";

import { battles, formatRate, starRate, type ArmyRow } from "../lib/army-stats";
import type { ArmyVariantExamples } from "../lib/contracts";
import type { VariantExamplesResponse } from "../routes/army-variant-examples";
import { ArmyPortraits } from "./ArmyPortraits";

/**
 * An opened army on the Armies page: a sheet over the list (from the bottom
 * on a phone, the side when wide) with the army's numbers, then its variants.
 * The first variant opens with its most-used armies; a tap opens another.
 */
export function ArmyDetail({
  row,
  variants,
  audience,
  examplesUrl,
  onClose,
}: {
  row: ArmyRow;
  /** The army's variants in the list's order, its Other last. */
  variants: ArmyRow[];
  /** Whose attacks these are, such as "top-1,000 players". */
  audience: string;
  examplesUrl: (variant: ArmyRow) => string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [openVariant, setOpenVariant] = useState<string | null>(variants[0]?.key ?? null);
  useEffect(() => {
    const sheet = dialog.current;
    if (sheet && !sheet.open) sheet.showModal();
  }, []);
  // A variant opened under the sheet's heading or low on the screen scrolls
  // up to it, as the one closing above it moves it.
  const tapped = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const toggle = tapped.current;
    tapped.current = null;
    const head = dialog.current?.querySelector(".analytics-sheet-head");
    if (!toggle || !head) return;
    const { top } = toggle.getBoundingClientRect();
    if (top < head.getBoundingClientRect().bottom || top > window.innerHeight * 0.6)
      toggle.scrollIntoView({
        block: "start",
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
      });
  }, [openVariant]);
  const titleId = `${row.key}-title`;
  return (
    <dialog
      ref={dialog}
      className="analytics-sheet"
      aria-labelledby={titleId}
      onClose={onClose}
      // A tap on the dimmed page around the sheet closes it.
      onClick={(event) => {
        if (event.target === event.currentTarget) event.currentTarget.close();
      }}
    >
      <div className="analytics-sheet-body">
        <header className="analytics-sheet-head">
          <h2 id={titleId}>{row.label}</h2>
          <button
            type="button"
            className="analytics-sheet-close"
            onClick={() => dialog.current?.close()}
          >
            <span aria-hidden="true">✕</span> Close
          </button>
        </header>
        <p className="analytics-sheet-note">
          {formatRate(row.usageRate)} of attacks by {audience}
        </p>
        <dl className="analytics-sheet-stats">
          <div>
            <dt>3-star</dt>
            <dd>{formatRate(starRate(row, 3))}</dd>
          </div>
          <div>
            <dt>Avg. trophies</dt>
            <dd>{row.averageTrophies?.toFixed(1) ?? "–"}</dd>
          </div>
          <div>
            <dt>Avg. stars</dt>
            <dd>{row.averageStars?.toFixed(2) ?? "–"}</dd>
          </div>
          <div>
            <dt>Battles</dt>
            <dd>{row.usageCount.toLocaleString()}</dd>
          </div>
        </dl>
        <p className="analytics-sheet-note">
          2-star {formatRate(starRate(row, 2))} · 1-star {formatRate(starRate(row, 1))}
          {row.averageDestruction != null
            ? ` · ${row.averageDestruction.toFixed(1)}% destruction`
            : null}
          {" · "}
          {row.averageMissTrophies == null
            ? "no misses"
            : `misses ${row.averageMissTrophies.toFixed(1)} trophies`}
        </p>
        <h3 className="analytics-sheet-section">Variants</h3>
        <p className="analytics-sheet-note">
          Each variant&rsquo;s share of {row.label} attacks.
        </p>
        <ol className="analytics-variants">
          {variants.map((variant) => {
            const open = openVariant === variant.key;
            return (
              <li key={variant.key} className="analytics-variant" data-open={open}>
                <button
                  type="button"
                  className="analytics-variant-toggle"
                  aria-expanded={open}
                  title={`${open ? "Hide" : "Show"} ${variant.label} armies`}
                  onClick={(event) => {
                    tapped.current = open ? null : event.currentTarget;
                    setOpenVariant(open ? null : variant.key);
                  }}
                >
                  <span className="analytics-variant-name">{variant.label}</span>
                  <strong className="analytics-variant-share">
                    {formatRate(variant.usageRate)}
                  </strong>
                  <span className="meter" aria-hidden="true">
                    <span
                      style={
                        { "--value": formatRate(variant.usageRate) } as CSSProperties
                      }
                    />
                  </span>
                  <span className="analytics-variant-stats">
                    3-star {formatRate(starRate(variant, 3))}
                    {variant.averageTrophies != null
                      ? ` · ${variant.averageTrophies.toFixed(1)} trophies`
                      : null}
                    {` · ${battles(variant.usageCount)}`}
                  </span>
                  <span className="analytics-chevron" aria-hidden="true" />
                </button>
                {open ? (
                  <VariantExamples
                    id={variant.key}
                    label={variant.label}
                    attacks={variant.usageCount}
                    url={examplesUrl(variant)}
                  />
                ) : null}
              </li>
            );
          })}
        </ol>
      </div>
    </dialog>
  );
}

/** An opened variant's most-used armies, loaded from the address the page gives. */
function VariantExamples({
  id,
  label,
  attacks,
  url,
}: {
  id: string;
  label: string;
  attacks: number;
  url: string;
}) {
  // Loaded again each time the variant opens; closing it drops what loaded.
  const fetcher = useFetcher<VariantExamplesResponse>({ key: url });
  const { load } = fetcher;
  useEffect(() => {
    void load(url);
  }, [load, url]);
  const examples = fetcher.data?.examples;
  return (
    <div
      className="analytics-examples"
      aria-live="polite"
      aria-busy={fetcher.state !== "idle"}
    >
      {examples === undefined ? (
        <p className="analytics-examples-rule">Loading armies…</p>
      ) : examples === null ? (
        <p className="analytics-examples-rule">
          These armies could not load. Close and open {label} to try again.
        </p>
      ) : (
        <ExampleArmies id={id} label={label} attacks={attacks} examples={examples} />
      )}
    </div>
  );
}

/** What decides the variant, then each group of alike armies with its
 * share of the variant's attacks, its attacks and Copy army. */
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
      <p className="analytics-examples-rule">
        {examples.rule} Near-identical armies count as one.
      </p>
      {examples.groups.length ? (
        <ol className="analytics-examples-list">
          {examples.groups.map((group, index) => (
            <li key={group.armyShareCode}>
              <div className="analytics-example-head">
                <p className="analytics-examples-share">
                  <strong>
                    {((attacks ? group.attacks / attacks : 0) * 100).toFixed(1)}%
                  </strong>{" "}
                  {group.attacks.toLocaleString()}{" "}
                  {group.attacks === 1 ? "attack" : "attacks"}
                </p>
                <a
                  className="battle-army"
                  href={`https://link.clashofclans.com/en?action=CopyArmy&army=${encodeURIComponent(group.armyShareCode)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Copy ${label} army ${index + 1}, opens in a new tab`}
                >
                  Copy army
                </a>
              </div>
              <ArmyPortraits
                id={`${id}-army-${index + 1}`}
                code={group.armyShareCode}
                names={examples.armyUnits}
              />
            </li>
          ))}
        </ol>
      ) : (
        <p className="analytics-examples-rule">No armies to show for these players.</p>
      )}
    </>
  );
}
