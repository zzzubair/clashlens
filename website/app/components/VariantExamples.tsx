import { useEffect, useRef, useState, type CSSProperties } from "react";

import { battles, formatRate, starRate, type ArmyRow } from "../lib/army-stats";
import type { ArmyVariantExamples } from "../lib/contracts";
import type { VariantExamplesResponse } from "../lib/variant-examples-data";
import { ArmyPortraits } from "./ArmyPortraits";

export type VariantExamplesRead =
  | { phase: "loading" }
  | { phase: "failed" }
  | { phase: "ready"; examples: ArmyVariantExamples };

export function variantExamplesRead(
  answer: { examples: ArmyVariantExamples | null } | undefined,
): VariantExamplesRead {
  const examples = answer?.examples;
  if (examples === undefined) return { phase: "loading" };
  return examples === null ? { phase: "failed" } : { phase: "ready", examples };
}

export function ArmyDetail({
  row,
  variants,
  audience,
  examplesUrl,
  onClose,
}: {
  row: ArmyRow;
  variants: ArmyRow[];
  audience: string;
  examplesUrl?: (variant: ArmyRow) => string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [openVariant, setOpenVariant] = useState<string | null>(null);
  useEffect(() => {
    const sheet = dialog.current;
    if (sheet && !sheet.open) sheet.showModal();
  }, []);
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
  const shareMax = Math.max(0, ...variants.map((variant) => variant.usageRate));
  return (
    <dialog
      ref={dialog}
      className="analytics-sheet"
      aria-labelledby={titleId}
      onClose={onClose}
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
            <dt>Trophies per attack</dt>
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
            ? "all 3 stars"
            : `${row.averageMissTrophies.toFixed(1)} trophies below 3 stars`}
        </p>
        <h3 className="analytics-sheet-section">Variants</h3>
        <p className="analytics-sheet-note">
          Each variant&rsquo;s share of {row.label} attacks.
        </p>
        <ol className="analytics-variants">
          {variants.map((variant) => {
            const open = openVariant === variant.key;
            const numbers = (
              <>
                <span className="analytics-variant-name">{variant.label}</span>
                <strong className="analytics-variant-share">
                  {formatRate(variant.usageRate)}
                </strong>
                <span className="meter" aria-hidden="true">
                  <span
                    style={
                      {
                        "--value": `${shareMax && (variant.usageRate / shareMax) * 100}%`,
                      } as CSSProperties
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
              </>
            );
            return (
              <li key={variant.key} className="analytics-variant" data-open={open}>
                {examplesUrl ? (
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
                    {numbers}
                    <span className="analytics-chevron" aria-hidden="true" />
                  </button>
                ) : (
                  <div className="analytics-variant-toggle" data-static="">
                    {numbers}
                  </div>
                )}
                {open && examplesUrl ? (
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
  const [answer, setAnswer] = useState<VariantExamplesResponse | undefined>(undefined);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setAnswer(undefined);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);
    let active = true;
    void fetch(url, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json() as Promise<VariantExamplesResponse>;
      })
      .then((payload) => {
        if (active) setAnswer(payload);
      })
      .catch(() => {
        if (active) setAnswer({ examples: null });
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      active = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [url, attempt]);
  const read = variantExamplesRead(answer);
  return (
    <div
      className="analytics-examples"
      aria-live="polite"
      aria-busy={read.phase === "loading"}
    >
      {read.phase === "loading" ? (
        <p className="analytics-examples-rule">Loading armies…</p>
      ) : read.phase === "failed" ? (
        <p className="analytics-examples-rule">
          These armies could not load.{" "}
          <button
            type="button"
            className="button button-secondary"
            onClick={() => setAttempt((count) => count + 1)}
          >
            Try again
          </button>
        </p>
      ) : (
        <ExampleArmies id={id} label={label} attacks={attacks} examples={read.examples} />
      )}
    </div>
  );
}

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
  const [all, setAll] = useState(false);
  const more = examples.groups.length - 1;
  return (
    <>
      <p className="analytics-examples-rule">
        {examples.rule} Near-identical armies count as one.
      </p>
      {examples.groups.length ? (
        <ol className="analytics-examples-list">
          {examples.groups.map((group, index) => (
            <li key={group.armyShareCode} hidden={index > 0 && !all}>
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
      {more > 0 && !all ? (
        <button
          type="button"
          className="button button-secondary analytics-examples-more"
          onClick={() => setAll(true)}
        >
          Show {more} more {more === 1 ? "army" : "armies"}
        </button>
      ) : null}
    </>
  );
}
