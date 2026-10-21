import { Link } from "react-router";

import { averageLost, heldText, type BaseArmy, type Notebook } from "../lib/notebook";
import { BaseTile } from "./NotebookParts";

/** The Season's Top 3 bases on the Notebook: fewest trophies lost a defense
 * among bases with at least 16 defenses. Hidden until there is a base. */
export function TopBases({ notebook }: { notebook: Notebook }) {
  if (notebook.bases.length === 0) return null;
  const bases = new Map(notebook.bases.map((base) => [base.id, base]));
  return (
    <section className="nb-section" aria-labelledby="nb-top">
      <div className="nb-section-head">
        <h2 id="nb-top">Top 3 bases this Season</h2>
      </div>
      {notebook.top.length ? (
        <ol className="nb-list nb-top">
          {notebook.top.map((code, index) => {
            const base = bases.get(code);
            if (!base) return null;
            const { record } = base;
            return (
              <li key={code}>
                <Link to={`/notebook/bases/${code}`} className="nb-row">
                  <span className="nb-top-place" aria-hidden="true">
                    {index + 1}
                  </span>
                  <BaseTile name={base.name} />
                  <span className="nb-row-text">
                    <b>{base.name}</b>
                    <small>
                      {heldText(record)} · {record.days}{" "}
                      {record.days === 1 ? "day" : "days"}
                    </small>
                  </span>
                  <span className="nb-lost">
                    {averageLost(record)}
                    <small>a defense</small>
                  </span>
                </Link>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="nb-hint">
          A base joins once it has 16 defenses on Legend days, about 2 days of running it.
        </p>
      )}
    </section>
  );
}

/** The base page's Armies section: each army type with at least 3 defenses
 * on the base, best first, worst last. */
export function BaseArmies({
  armies,
  defenses,
}: {
  armies: BaseArmy[] | null;
  defenses: number;
}) {
  const ranked = armies?.reduce((sum, army) => sum + army.defenses, 0) ?? 0;
  return (
    <section className="nb-base-part" aria-labelledby="nb-armies-title">
      <h2 id="nb-armies-title">Armies, best to worst</h2>
      {armies === null ? (
        <p className="nb-hint">
          Armies are being sorted again after a rules update. Check back in a few minutes.
        </p>
      ) : armies.length ? (
        <>
          <ol className="nb-per-account nb-armies">
            {armies.map((army, index) => (
              <li key={army.name}>
                <span className="nb-army-name">
                  {armies.length > 1 && index === 0 ? (
                    <span className="nb-army-tag nb-army-best">Best</span>
                  ) : null}
                  {armies.length > 1 && index === armies.length - 1 ? (
                    <span className="nb-army-tag nb-army-worst">Worst</span>
                  ) : null}
                  <b>{army.name}</b>
                </span>
                <span className="nb-army-lost">{averageLost(army)} a defense</span>
                <small>
                  held {army.held} of {army.defenses}
                </small>
              </li>
            ))}
          </ol>
          <p className="nb-hint">
            {ranked} of {defenses} defenses ranked. An army needs 3 defenses on this base;
            Other and unreadable armies are left out.
          </p>
        </>
      ) : (
        <p className="nb-hint">
          No army has 3 defenses on this base yet. Other and unreadable armies are left
          out.
        </p>
      )}
    </section>
  );
}
