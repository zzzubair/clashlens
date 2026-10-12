import { useEffect, useRef, useState, type ReactNode } from "react";
import { useFetcher } from "react-router";

import type { CardId, DashboardSettings, GroupWindow } from "../lib/dashboard";
import {
  CARDS,
  CARD_SIZE_LABELS,
  CUTOFF_LINES,
  CUTOFF_PICKS,
  DEFAULT_CUTOFFS,
  MAX_GOAL,
  addableCards,
  serializeSettings,
  shownCards,
} from "../lib/dashboard";
import type { CardDataMap, CardDataSet } from "../lib/dashboard-cards";
import type { DashboardActionData } from "../routes/dashboard";
import {
  ClockCard,
  CutoffsCard,
  GoalCard,
  GroupWatchCard,
  RivalsCard,
} from "./BoardCards";
import { DashboardIcon } from "./DashboardIcon";
import { CardHead, CardNote } from "./DashboardParts";
import {
  HeatMapCard,
  OffenseConsistencyCard,
  RecentFormCard,
  SeasonGraphCard,
} from "./SeasonCards";

interface ViewContext {
  nowMs: number | null;
  timeZone: string;
  saving: boolean;
  pickGroup: (groupId: string) => void;
}

/** How each built card draws its data. A card added later adds its view here. */
const CARD_VIEWS: {
  [Card in keyof CardDataMap]: (
    data: CardDataMap[Card],
    context: ViewContext,
  ) => ReactNode;
} = {
  "legend-clock": (data, context) => (
    <ClockCard data={data} nowMs={context.nowMs} timeZone={context.timeZone} />
  ),
  goal: (data) => <GoalCard data={data} />,
  "rivals-watch": (data, context) => <RivalsCard data={data} nowMs={context.nowMs} />,
  "heat-map": (data) => <HeatMapCard data={data} />,
  cutoffs: (data, context) => <CutoffsCard data={data} nowMs={context.nowMs} />,
  "season-graph": (data) => <SeasonGraphCard data={data} />,
  "recent-form": (data) => <RecentFormCard data={data} />,
  "offense-consistency": (data) => <OffenseConsistencyCard data={data} />,
  "group-watch": (data, context) => (
    <GroupWatchCard data={data} onPick={context.pickGroup} saving={context.saving} />
  ),
};

function hasView(card: CardId): card is keyof CardDataMap {
  return Object.hasOwn(CARD_VIEWS, card);
}

function cardBody(card: CardId, data: CardDataSet, context: ViewContext): ReactNode {
  const title = CARDS[card].title;
  if (!hasView(card)) return <CardHead title={title} />;
  const loaded = data[card];
  if (loaded === undefined)
    return (
      <>
        <CardHead title={title} />
        <CardNote>This card loads when you press Done.</CardNote>
      </>
    );
  if (loaded === null)
    return (
      <>
        <CardHead title={title} />
        <CardNote>
          This card could not be loaded right now. Reload the page to try again.
        </CardNote>
      </>
    );
  const view = CARD_VIEWS[card] as (
    value: typeof loaded,
    context: ViewContext,
  ) => ReactNode;
  return view(loaded, context);
}

function deviceTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function moved(cards: CardId[], index: number, step: -1 | 1): CardId[] {
  const next = [...cards];
  const target = index + step;
  if (target < 0 || target >= next.length) return cards;
  [next[index], next[target]] = [next[target] as CardId, next[index] as CardId];
  return next;
}

/** Card settings, opened from "⚙ Settings" while customising. */
function SettingsDialog({
  card,
  settings,
  saving,
  error,
  onSave,
  onClose,
}: {
  card: CardId;
  settings: DashboardSettings;
  saving: boolean;
  error: string | null | undefined;
  onSave: (next: DashboardSettings) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  // Only this dialog's own save failing shows here, not an earlier one.
  const [tried, setTried] = useState(false);
  const [goal, setGoal] = useState(String(settings.goal));
  const [lines, setLines] = useState<number[]>(settings.cutoffs);
  const [groupWindow, setGroupWindow] = useState<GroupWindow>(settings.groupWindow);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const goalNumber = Number(goal);
  const goalValid = /^\d+$/.test(goal) && goalNumber >= 1 && goalNumber <= MAX_GOAL;
  const valid =
    card === "goal"
      ? goalValid
      : card === "cutoffs"
        ? lines.length === CUTOFF_PICKS
        : true;
  const save = () => {
    if (!valid) return;
    setTried(true);
    onSave(
      card === "goal"
        ? { ...settings, goal: goalNumber }
        : card === "cutoffs"
          ? { ...settings, cutoffs: [...lines].sort((a, b) => a - b) }
          : { ...settings, groupWindow },
    );
  };
  return (
    <dialog
      ref={dialog}
      className="dash-dialog dash-settings"
      aria-labelledby="dash-settings-title"
      onClose={onClose}
    >
      <form
        method="dialog"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <header className="dash-dialog-head">
          <h2 id="dash-settings-title">{CARDS[card].title} settings</h2>
          <button
            type="button"
            className="dash-icon-button"
            aria-label="Close"
            onClick={() => dialog.current?.close()}
          >
            <DashboardIcon name="x" />
          </button>
        </header>
        {card === "goal" ? (
          <label className="dash-field">
            Finish the Season in the top
            <input
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_GOAL}
              step={1}
              value={goal}
              autoFocus
              aria-describedby="dash-goal-help"
              onChange={(event) => setGoal(event.currentTarget.value)}
            />
            <span id="dash-goal-help" className={goalValid ? "dash-muted" : "negative"}>
              {goalValid
                ? `Any rank from 1 to ${MAX_GOAL.toLocaleString("en-US")}`
                : `Enter a whole number from 1 to ${MAX_GOAL.toLocaleString("en-US")}`}
            </span>
          </label>
        ) : card === "cutoffs" ? (
          <fieldset className="dash-field">
            <legend>
              Pick {CUTOFF_PICKS} lines ·{" "}
              <b>
                {lines.length} of {CUTOFF_PICKS}
              </b>
            </legend>
            <div className="dash-chips">
              {CUTOFF_LINES.map((line) => {
                const on = lines.includes(line);
                return (
                  <button
                    key={line}
                    type="button"
                    className="dash-chip-button"
                    aria-pressed={on}
                    disabled={!on && lines.length >= CUTOFF_PICKS}
                    onClick={() =>
                      setLines(
                        on ? lines.filter((item) => item !== line) : [...lines, line],
                      )
                    }
                  >
                    {on ? "✓ " : ""}Top {line.toLocaleString("en-US")}
                  </button>
                );
              })}
            </div>
            <button
              type="button"
              className="button button-secondary dash-small-button"
              onClick={() => setLines([...DEFAULT_CUTOFFS])}
            >
              Reset to default
            </button>
          </fieldset>
        ) : (
          <fieldset className="dash-field">
            <legend>Show</legend>
            <div className="dash-chips">
              {(
                [
                  ["7", "Last 7 days"],
                  ["season", "Whole Season"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className="dash-chip-button"
                  aria-pressed={groupWindow === value}
                  onClick={() => setGroupWindow(value)}
                >
                  {groupWindow === value ? "✓ " : ""}
                  {label}
                </button>
              ))}
            </div>
          </fieldset>
        )}
        {tried && error ? (
          <p className="negative" role="alert">
            {error}
          </p>
        ) : null}
        <div className="dash-dialog-actions">
          <button
            type="submit"
            className="button button-primary"
            disabled={!valid || saving}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

/** "+ Add a card": every built card not on the dashboard, with its size and one line. */
function AddSheet({
  cards,
  saving,
  onAdd,
  onClose,
}: {
  cards: CardId[];
  saving: boolean;
  onAdd: (card: CardId) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="dash-dialog dash-add-sheet"
      aria-labelledby="dash-add-title"
      onClose={onClose}
    >
      <header className="dash-dialog-head">
        <h2 id="dash-add-title">Add a card</h2>
        <button
          type="button"
          className="button button-secondary dash-small-button"
          onClick={() => dialog.current?.close()}
        >
          Done
        </button>
      </header>
      {cards.length ? (
        <ul className="dash-add-list">
          {cards.map((card) => (
            <li key={card}>
              <div>
                <b>{CARDS[card].title}</b>{" "}
                <span className="dash-size-chip">
                  {CARD_SIZE_LABELS[CARDS[card].size]}
                </span>
                <p className="dash-muted">{CARDS[card].description}</p>
              </div>
              <button
                type="button"
                className="button button-primary dash-small-button"
                aria-label={`Add ${CARDS[card].title}`}
                disabled={saving}
                onClick={() => onAdd(card)}
              >
                Add
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="dash-muted dash-add-none">
          Every card is already on your dashboard.
        </p>
      )}
    </dialog>
  );
}

/** The first-visit pop-up: our layout or an empty one. */
function FirstVisit({
  saving,
  onPick,
}: {
  saving: boolean;
  onPick: (choice: "default" | "own") => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="dash-dialog dash-welcome"
      aria-labelledby="dash-welcome-title"
    >
      <h2 id="dash-welcome-title">Your dashboard</h2>
      <p className="dash-muted">
        Pick a start. Change it any time with <DashboardIcon name="gear" /> Customise
        dashboard.
      </p>
      <div className="dash-welcome-options">
        <button
          type="button"
          className="dash-welcome-option"
          disabled={saving}
          onClick={() => {
            onPick("default");
            dialog.current?.close();
          }}
        >
          <span className="dash-welcome-badge">Recommended</span>
          <b>Use our layout</b>
          <span className="dash-muted">Our pick of cards, ready now</span>
        </button>
        <button
          type="button"
          className="dash-welcome-option"
          disabled={saving}
          onClick={() => {
            onPick("own");
            dialog.current?.close();
          }}
        >
          <b>
            <DashboardIcon name="plus" /> Build my own
          </b>
          <span className="dash-muted">Start empty and add cards</span>
        </button>
      </div>
    </dialog>
  );
}

/**
 * The divider with "⚙ Customise dashboard", the card grid and Customise.
 * Customise edits a draft of the card order; Done saves it. Card settings
 * and the group picked on Group watch save at once.
 */
export function Dashboard({
  settings,
  data,
  idempotencyKey,
}: {
  settings: DashboardSettings;
  data: CardDataSet;
  idempotencyKey: string;
}) {
  const fetcher = useFetcher<DashboardActionData>();
  const [draft, setDraft] = useState<DashboardSettings["layout"] | null>(null);
  const [adding, setAdding] = useState(false);
  const [editingSettings, setEditingSettings] = useState<CardId | null>(null);
  // Done waits for its own save's answer, not an earlier one.
  const [finishing, setFinishing] = useState<{
    after: DashboardActionData | undefined;
  } | null>(null);
  const [status, setStatus] = useState("");
  const [dragging, setDragging] = useState<CardId | null>(null);
  const [nowMs, setNowMs] = useState<number | null>(null);
  const [timeZone, setTimeZone] = useState("UTC");

  useEffect(() => {
    setTimeZone(deviceTimeZone());
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const saving = fetcher.state !== "idle";
  const saved = fetcher.state === "idle" && fetcher.data?.saved === true;
  useEffect(() => {
    if (
      finishing === null ||
      fetcher.state !== "idle" ||
      fetcher.data === finishing.after
    )
      return;
    setFinishing(null);
    if (fetcher.data?.saved) {
      setDraft(null);
      setAdding(false);
    }
  }, [finishing, fetcher.state, fetcher.data]);
  useEffect(() => {
    if (saved) setEditingSettings(null);
  }, [saved]);

  const save = (next: DashboardSettings) =>
    fetcher.submit(
      {
        idempotencyKey: fetcher.data?.idempotencyKey ?? idempotencyKey,
        settings: JSON.stringify(serializeSettings(next)),
      },
      { method: "post" },
    );

  const editing = draft !== null;
  const cards = shownCards(editing ? draft : settings.layout);
  const change = (next: CardId[], message: string) => {
    setDraft(next);
    setStatus(message);
  };

  // Dragging: the card under the pointer makes room; the page scrolls near its edges.
  useEffect(() => {
    if (dragging === null) return;
    const move = (event: PointerEvent) => {
      const edge = 80;
      if (event.clientY < edge) window.scrollBy(0, -12);
      else if (event.clientY > window.innerHeight - edge) window.scrollBy(0, 12);
      const target = document
        .elementFromPoint(event.clientX, event.clientY)
        ?.closest<HTMLElement>(".dash-grid [data-card]");
      const over = target?.dataset.card as CardId | undefined;
      if (!target || !over || over === dragging) return;
      const box = target.getBoundingClientRect();
      const stacked =
        CARDS[over].size === "l" || window.matchMedia("(max-width: 40rem)").matches;
      const before = stacked
        ? event.clientY < box.top + box.height / 2
        : event.clientX < box.left + box.width / 2;
      setDraft((current) => {
        const list = shownCards(current ?? null).filter((card) => card !== dragging);
        list.splice(list.indexOf(over) + (before ? 0 : 1), 0, dragging);
        const was = shownCards(current ?? null);
        return list.join() === was.join() ? current : list;
      });
    };
    const stop = () => {
      setDragging(null);
      setStatus(`Moved ${CARDS[dragging].title}`);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };
  }, [dragging]);

  const context: ViewContext = {
    nowMs,
    timeZone,
    saving,
    pickGroup: (groupId) => save({ ...settings, group: groupId }),
  };
  const firstVisit = settings.layout === null && !editing;
  const error = fetcher.state === "idle" ? fetcher.data?.error : null;

  return (
    <>
      <div className="dash-divider">
        {!editing ? (
          <button
            type="button"
            className="button button-secondary dash-customise"
            disabled={saving}
            onClick={() => {
              setDraft(settings.layout ?? "default");
              setStatus("");
            }}
          >
            <DashboardIcon name="gear" /> Customise dashboard
          </button>
        ) : null}
      </div>
      {editing ? (
        <div className="dash-editbar" role="region" aria-label="Customise dashboard">
          <b className="dash-editbar-title">Customise dashboard</b>
          <span className="dash-editbar-state">
            {draft === "default" ? "Default layout" : "Your own layout"}
          </span>
          <span className="sr-only" role="status">
            {status}
          </span>
          <button
            type="button"
            className="button button-secondary"
            disabled={saving}
            onClick={() => setAdding(true)}
          >
            <DashboardIcon name="plus" /> Add a card
          </button>
          <button
            type="button"
            className="button button-secondary"
            disabled={saving || draft === "default"}
            onClick={() => {
              setDraft("default");
              setStatus("Back to the default layout");
            }}
          >
            Reset to default
          </button>
          <button
            type="button"
            className="button button-primary"
            disabled={saving}
            onClick={() => {
              setFinishing({ after: fetcher.data });
              save({ ...settings, layout: draft });
            }}
          >
            <DashboardIcon name="check" /> {finishing && saving ? "Saving…" : "Done"}
          </button>
          {error ? (
            <p className="dash-editbar-error" role="alert">
              {error}
            </p>
          ) : null}
        </div>
      ) : error ? (
        <p className="notice notice-warning" role="alert">
          {error}
        </p>
      ) : null}
      <div className={`dash-grid${editing ? " dash-grid-editing" : ""}`}>
        {cards.map((card, index) => (
          <section
            key={card}
            className={`dash-card dash-card-${CARDS[card].size}${dragging === card ? " dash-card-dragging" : ""}`}
            aria-label={CARDS[card].title}
            data-card={card}
          >
            {editing ? (
              <div className="dash-edit-strip">
                <button
                  type="button"
                  className="dash-grip"
                  aria-label={`Drag to move ${CARDS[card].title}`}
                  disabled={saving}
                  onPointerDown={(event) => {
                    if (event.button > 0 || saving) return;
                    event.preventDefault();
                    setDragging(card);
                  }}
                >
                  ⠿
                </button>
                <span className="dash-edit-name">{CARDS[card].title}</span>
                <span className="dash-size-chip">
                  {CARD_SIZE_LABELS[CARDS[card].size]}
                </span>
                <button
                  type="button"
                  className="dash-icon-button"
                  aria-label={`Move ${CARDS[card].title} earlier`}
                  disabled={saving || index === 0}
                  onClick={() =>
                    change(moved(cards, index, -1), `Moved ${CARDS[card].title} earlier`)
                  }
                >
                  <DashboardIcon name="up" />
                </button>
                <button
                  type="button"
                  className="dash-icon-button"
                  aria-label={`Move ${CARDS[card].title} later`}
                  disabled={saving || index === cards.length - 1}
                  onClick={() =>
                    change(moved(cards, index, 1), `Moved ${CARDS[card].title} later`)
                  }
                >
                  <DashboardIcon name="down" />
                </button>
                <button
                  type="button"
                  className="dash-icon-button"
                  aria-label={`Remove ${CARDS[card].title}`}
                  disabled={saving}
                  onClick={() =>
                    change(
                      cards.filter((item) => item !== card),
                      `Removed ${CARDS[card].title}. Add it back from Add a card.`,
                    )
                  }
                >
                  <DashboardIcon name="x" />
                </button>
              </div>
            ) : null}
            {editing && CARDS[card].settings ? (
              <button
                type="button"
                className="dash-card-settings"
                aria-label={`${CARDS[card].title} settings`}
                disabled={saving}
                onClick={() => setEditingSettings(card)}
              >
                <DashboardIcon name="gear" /> Settings
              </button>
            ) : null}
            {cardBody(card, data, context)}
          </section>
        ))}
      </div>
      {cards.length === 0 ? (
        <div className="dash-empty">
          <b>Your dashboard is empty</b>
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              if (!editing) setDraft([]);
              setAdding(true);
            }}
          >
            <DashboardIcon name="plus" /> Add a card
          </button>
        </div>
      ) : null}
      {adding && editing ? (
        <AddSheet
          cards={addableCards(cards)}
          saving={saving}
          onClose={() => setAdding(false)}
          onAdd={(card) =>
            change([...cards, card], `Added ${CARDS[card].title} at the end`)
          }
        />
      ) : null}
      {editingSettings ? (
        <SettingsDialog
          card={editingSettings}
          settings={settings}
          saving={saving}
          error={error}
          onSave={save}
          onClose={() => setEditingSettings(null)}
        />
      ) : null}
      {firstVisit ? (
        <FirstVisit
          saving={saving}
          onPick={(choice) => {
            if (choice === "default") return save({ ...settings, layout: "default" });
            save({ ...settings, layout: [] });
            setDraft([]);
            setAdding(true);
          }}
        />
      ) : null}
    </>
  );
}
