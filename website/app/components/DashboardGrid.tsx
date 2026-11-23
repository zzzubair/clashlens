import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { Await, Link, useFetcher, type FetcherWithComponents } from "react-router";

import type { BasesMode, CardId, DashboardSettings, GroupWindow } from "../lib/dashboard";
import {
  ACCOUNT_CARDS,
  CARDS,
  CARD_SIZE_LABELS,
  CUTOFF_LINES,
  CUTOFF_PICKS,
  DEFAULT_CUTOFFS,
  MAX_GOAL,
  addableCards,
  cardPlaces,
  dashboardQuery,
  serializeSettings,
  shownCards,
} from "../lib/dashboard";
import type { CardDataMap, CardReads } from "../lib/dashboard-cards";
import { RETRY_DEADLINE_MS } from "../lib/keep-page";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { DashboardActionData, DashboardLoaderData } from "../lib/dashboard-data";
import {
  ClockCard,
  CutoffsCard,
  GoalCard,
  GroupWatchCard,
  RivalsCard,
} from "./BoardCards";
import { ArmiesOnDefenseCard, ArmiesOnOffenseCard } from "./ArmyCards";
import { DashboardIcon } from "./DashboardIcon";
import {
  CARDS_FROM_LOAD,
  DASHBOARD_VIEWING,
  cardReads,
  cardRefreshStep,
  dashboardEditStep,
  outdatedCards,
  stalledCards,
  type DashboardEditEvent,
} from "./DashboardState";
import { CardHead, CardNote } from "./DashboardParts";
import { updatedAgo } from "../lib/age-format";
import { LegendDayCard, dayReadAt } from "./LegendDayCard";
import { OpponentsCard, useBasesDay, type BasesDay } from "./OpponentsCard";
import {
  HeatMapCard,
  OffenseConsistencyCard,
  RecentFormCard,
  SeasonGraphCard,
} from "./SeasonCards";

interface ViewContext {
  tag: string;
  viewAs: string | null;
  dayOver: boolean;
  nowMs: number | null;
  timeZone: string;
  basesMode: BasesMode;
  basesDay: BasesDay;
  saving: boolean;
  pickGroup: (groupId: string) => void;
  retryCard: (card: keyof CardDataMap) => void;
}

const CARD_VIEWS: {
  [Card in keyof CardDataMap]: (
    data: CardDataMap[Card],
    context: ViewContext,
  ) => ReactNode;
} = {
  "legend-day": (data, context) => <LegendDayCard {...data} nowMs={context.nowMs} />,
  bases: (data, context) => (
    <OpponentsCard
      dayNumber={data.dayNumber}
      days={context.basesDay}
      rows={data.rows}
      mode={context.basesMode}
      timeZone={context.timeZone}
    />
  ),
  "armies-defense": (data) => <ArmiesOnDefenseCard data={data} />,
  "armies-offense": (data) => <ArmiesOnOffenseCard data={data} />,
  "legend-clock": (data, context) => (
    <ClockCard data={data} nowMs={context.nowMs} timeZone={context.timeZone} />
  ),
  goal: (data) => <GoalCard data={data} />,
  "rivals-watch": (data) => <RivalsCard data={data} />,
  "heat-map": (data) => <HeatMapCard data={data} />,
  cutoffs: (data) => <CutoffsCard data={data} />,
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

const BOARD_TRIES = 3;

function ReadAgain<Data>({
  card,
  tag,
  viewAs,
  shown,
  children,
}: {
  card: keyof CardReads;
  tag: string;
  viewAs: string | null;
  shown: ReactNode;
  children: (data: Data) => ReactNode;
}) {
  const { load, data } = useFetcher<DashboardLoaderData>();
  useEffect(() => {
    void load(dashboardQuery({ player: tag, cards: card, board: "wait" }, viewAs));
  }, [load, tag, viewAs, card]);
  const read = (
    data?.kind === "signed-in" && data.selectedTag === tag ? data.cards[card] : null
  ) as Data | null | Promise<Data | null> | undefined;
  if (!read) return shown;
  return (
    <Suspense fallback={shown}>
      <Await resolve={read} errorElement={shown}>
        {(next: Data | null) => (next === null ? shown : children(next))}
      </Await>
    </Suspense>
  );
}

function cardBody(
  card: CardId,
  reads: CardReads | null,
  editing: boolean,
  context: ViewContext,
): ReactNode {
  const title = CARDS[card].title;
  if (context.dayOver || !hasView(card))
    return <CardHead icon={CARDS[card].icon} title={title} />;
  const note = (text: string) => (
    <>
      <CardHead icon={CARDS[card].icon} title={title} />
      <CardNote>{text}</CardNote>
    </>
  );
  if (context.viewAs !== null && ACCOUNT_CARDS.includes(card))
    return note("Your Watchlists stay off while you view as another player.");
  const failed = (
    <>
      <CardHead icon={CARDS[card].icon} title={title} />
      <CardNote>This card could not be loaded right now.</CardNote>
      <button
        type="button"
        className="button button-secondary dash-small-button"
        onClick={() => context.retryCard(card)}
      >
        Try again
      </button>
    </>
  );
  const loading = <div aria-busy="true">{note("Loading…")}</div>;
  const read = reads?.[card];
  if (read === undefined)
    return editing && reads !== null
      ? note("This card loads when you press Done.")
      : loading;
  const view = CARD_VIEWS[card] as (
    value: NonNullable<Awaited<typeof read>>,
    context: ViewContext,
  ) => ReactNode;
  const show = (loaded: NonNullable<Awaited<typeof read>>, tries = 0): ReactNode =>
    "boardPending" in loaded && tries < BOARD_TRIES ? (
      <ReadAgain
        card={card}
        tag={context.tag}
        viewAs={context.viewAs}
        shown={view(loaded, context)}
      >
        {(next: typeof loaded) => show(next, tries + 1)}
      </ReadAgain>
    ) : (
      view(loaded, context)
    );
  if (!(read instanceof Promise)) return read === null ? failed : show(read);
  return (
    <Suspense fallback={loading}>
      <Await resolve={read} errorElement={failed}>
        {(loaded: Awaited<typeof read>) => (loaded === null ? failed : show(loaded))}
      </Await>
    </Suspense>
  );
}

const FRESH_CARDS = ["legend-day", "legend-clock", "rivals-watch", "cutoffs"] as const;

export function readAt(card: (typeof FRESH_CARDS)[number], data: unknown): number | null {
  if (data === null || typeof data !== "object") return null;
  if (card === "legend-day") {
    const { day, rangeAtMs } = data as CardDataMap["legend-day"];
    return dayReadAt(day, rangeAtMs);
  }
  if (card === "legend-clock") return (data as CardDataMap["legend-clock"]).observedAtMs;
  return (data as CardDataMap["rivals-watch" | "cutoffs"]).live?.observedAtMs ?? null;
}

function Freshness({ reads, nowMs }: { reads: CardReads | null; nowMs: number | null }) {
  const card = FRESH_CARDS.find((name) => reads?.[name] !== undefined);
  const read = card ? reads?.[card] : undefined;
  if (!card || !read) return null;
  const line = (data: unknown) => {
    const text = updatedAgo(readAt(card, data), nowMs);
    return text ? <span className="dash-fresh">{text.replace(/^u/, "U")}</span> : null;
  };
  if (!(read instanceof Promise)) return line(read);
  return (
    <Suspense fallback={null}>
      <Await resolve={read} errorElement={null}>
        {line}
      </Await>
    </Suspense>
  );
}

function DashboardTools({
  tag,
  saving,
  fresh,
  customise,
}: {
  tag: string;
  saving: boolean;
  fresh: ReactNode;
  customise: () => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape") return;
        toggle.current?.focus();
      } else if (box.current?.contains(event.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  return (
    <div className={`dash-divider${open ? " is-open" : ""}`} ref={box}>
      <button
        ref={toggle}
        type="button"
        className="dash-more"
        aria-label="Dashboard options"
        aria-expanded={open}
        aria-controls="dash-tools"
        onClick={() => setOpen(!open)}
      >
        <DashboardIcon name="gear" />
        <span className="dash-more-label">Options</span>
      </button>
      <div className="dash-tools" id="dash-tools">
        {fresh}
        <Link className="dash-tool" to={canonicalPlayerPath(tag)}>
          <DashboardIcon name="open" /> Player page
        </Link>
        <button
          type="button"
          className="dash-tool dash-customise"
          aria-label="Customise dashboard"
          disabled={saving}
          onClick={() => {
            setOpen(false);
            customise();
          }}
        >
          <DashboardIcon name="gear" /> Customise
        </button>
      </div>
    </div>
  );
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

function Choice<Value extends string>({
  legend,
  options,
  value,
  onChange,
}: {
  legend: string;
  options: readonly (readonly [Value, string])[];
  value: Value;
  onChange: (value: Value) => void;
}) {
  return (
    <fieldset className="dash-field">
      <legend>{legend}</legend>
      <div className="dash-chips">
        {options.map(([option, label]) => (
          <button
            key={option}
            type="button"
            className="dash-chip-button"
            aria-pressed={value === option}
            onClick={() => onChange(option)}
          >
            {value === option ? "✓ " : ""}
            {label}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

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
  const [tried, setTried] = useState(false);
  const [goal, setGoal] = useState(String(settings.goal));
  const [lines, setLines] = useState<number[]>(settings.cutoffs);
  const [groupWindow, setGroupWindow] = useState<GroupWindow>(settings.groupWindow);
  const [basesMode, setBasesMode] = useState<BasesMode>(settings.basesMode);
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
          : card === "bases"
            ? { ...settings, basesMode }
            : { ...settings, groupWindow },
    );
  };
  return (
    <dialog
      ref={dialog}
      className="dash-dialog dash-settings"
      aria-labelledby="dash-settings-title"
      onClose={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) dialog.current?.close();
      }}
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
        ) : card === "bases" ? (
          <Choice
            legend="Rate each base by"
            options={[
              ["trophies", "Trophies"],
              ["held", "Held %"],
            ]}
            value={basesMode}
            onChange={setBasesMode}
          />
        ) : (
          <Choice
            legend="Show"
            options={[
              ["7", "Last 7 days"],
              ["season", "Whole Season"],
            ]}
            value={groupWindow}
            onChange={setGroupWindow}
          />
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
      onClick={(event) => {
        if (event.target === event.currentTarget) dialog.current?.close();
      }}
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
  const choose = (choice: "default" | "own") => {
    onPick(choice);
    dialog.current?.close();
  };
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
          onClick={() => choose("default")}
        >
          <span className="dash-welcome-badge">Recommended</span>
          <b>Use our layout</b>
          <span className="dash-muted">Our pick of cards, ready now</span>
        </button>
        <button
          type="button"
          className="dash-welcome-option"
          disabled={saving}
          onClick={() => choose("own")}
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

export function saveSettings(
  fetcher: Pick<FetcherWithComponents<DashboardActionData>, "data" | "submit">,
  viewAs: string | null,
  idempotencyKey: string,
  next: DashboardSettings,
): void {
  if (viewAs !== null) return;
  void fetcher.submit(
    {
      idempotencyKey: fetcher.data?.idempotencyKey ?? idempotencyKey,
      settings: JSON.stringify(serializeSettings(next)),
    },
    { method: "post" },
  );
}

export function Dashboard({
  tag,
  viewAs = null,
  settings: accountSettings,
  data,
  dayOver,
  idempotencyKey,
  fetcher,
}: {
  tag: string;
  viewAs?: string | null;
  settings: DashboardSettings;
  data: CardReads | null;
  dayOver: boolean;
  idempotencyKey: string;
  fetcher: FetcherWithComponents<DashboardActionData>;
}) {
  const refresher = useFetcher<DashboardLoaderData>();
  const [ownLayout, setOwnLayout] = useState<DashboardSettings["layout"]>(null);
  const settings = useMemo(
    () =>
      viewAs === null || ownLayout === null
        ? accountSettings
        : { ...accountSettings, layout: ownLayout },
    [accountSettings, viewAs, ownLayout],
  );
  const [cardData, dispatchCards] = useReducer(cardRefreshStep, CARDS_FROM_LOAD);
  const { stale } = cardData;
  const readWith = useRef({ data, settings });
  const [edit, setEdit] = useState(DASHBOARD_VIEWING);
  const dispatch = useCallback(
    (event: DashboardEditEvent) => setEdit((state) => dashboardEditStep(state, event)),
    [],
  );
  const editing = edit.mode === "editing";
  const draft = editing ? edit.draft : null;
  const sheet = editing ? edit.sheet : null;
  const adding = sheet?.kind === "adding";
  const editingSettings = sheet?.kind === "settings" ? sheet.card : null;
  const dragging = editing ? edit.dragging : null;
  const finishing = editing ? edit.finishing : null;
  const [nowMs, setNowMs] = useState<number | null>(null);
  const [timeZone, setTimeZone] = useState("UTC");
  const basesDay = useBasesDay(tag);

  useEffect(() => {
    setTimeZone(deviceTimeZone());
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const saving = fetcher.state === "submitting";
  const answer = saving ? undefined : fetcher.data;
  const saved = answer?.saved === true;
  const reads = cardReads(data, cardData);

  useEffect(() => {
    const before = readWith.current;
    readWith.current = { data, settings };
    if (before.data !== data) {
      dispatchCards({ type: "newLoad" });
      if (viewAs !== null && data !== null) {
        const cards = outdatedCards(settings, settings, data, hasView);
        dispatchCards({ type: "outdated", cards });
      }
    } else if (reads !== null && before.settings !== settings) {
      const cards = outdatedCards(before.settings, settings, reads, hasView);
      dispatchCards({ type: "outdated", cards });
    }
  }, [data, settings, reads, viewAs]);
  const { load } = refresher;
  const loading = data === null;
  useEffect(() => {
    if (stale.length === 0 || loading) return;
    dispatchCards({ type: "requested", cards: stale });
    void load(dashboardQuery({ player: tag, cards: stale.join(",") }, viewAs));
    const timer = window.setTimeout(
      () => dispatchCards({ type: "answered", answer: stalledCards(), tag }),
      RETRY_DEADLINE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [stale, tag, viewAs, loading, load]);
  useEffect(() => {
    const answer = refresher.data;
    if (refresher.state === "idle" && answer)
      dispatchCards({ type: "answered", answer, tag });
  }, [refresher.state, refresher.data, tag]);
  useEffect(() => {
    if (!saving) dispatch({ type: "answered", answer: fetcher.data });
  }, [finishing, saving, fetcher.data]);
  useEffect(() => {
    if (saved) dispatch({ type: "saved" });
  }, [saved]);

  const save = (next: DashboardSettings) =>
    saveSettings(fetcher, viewAs, idempotencyKey, next);

  const cards = shownCards(editing ? draft : settings.layout);
  const wide = cardPlaces(cards, 3, !editing);
  const medium = cardPlaces(cards, 2, !editing);
  const change = (next: CardId[], message: string) =>
    dispatch({ type: "change", draft: next, status: message });
  const cancel = () => dispatch({ type: "cancel" });
  const sheetOpen = adding || editingSettings !== null;
  useEffect(() => {
    if (!editing || sheetOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) cancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

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
      dispatch({ type: "dragOver", over, before });
    };
    const stop = () => dispatch({ type: "dragEnd" });
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
    tag,
    viewAs,
    dayOver,
    nowMs,
    timeZone,
    basesMode: settings.basesMode,
    basesDay,
    saving,
    pickGroup: (groupId) => save({ ...settings, group: groupId }),
    retryCard: (card) => dispatchCards({ type: "outdated", cards: [card] }),
  };
  const firstVisit = settings.layout === null && !editing && viewAs === null;
  const error = answer?.error;

  return (
    <>
      {!editing ? (
        <DashboardTools
          tag={tag}
          saving={saving}
          fresh={
            dayOver || cards.includes("legend-day") ? null : (
              <Freshness reads={reads} nowMs={nowMs} />
            )
          }
          customise={() => dispatch({ type: "customise", layout: settings.layout })}
        />
      ) : null}
      {editing ? (
        <div className="dash-editbar" role="region" aria-label="Customise dashboard">
          <b className="dash-editbar-title">Customise dashboard</b>
          <span className="dash-editbar-state">
            {draft === "default" ? "Default layout" : "Your own layout"}
          </span>
          <span className="sr-only" role="status">
            {edit.status}
          </span>
          <button
            type="button"
            className="button button-secondary"
            disabled={saving}
            onClick={() => dispatch({ type: "openAdd" })}
          >
            <DashboardIcon name="plus" /> Add
            <span className="dash-editbar-long"> a card</span>
          </button>
          <button
            type="button"
            className="button button-secondary"
            disabled={saving || draft === "default"}
            onClick={() => dispatch({ type: "useDefault" })}
          >
            Use default<span className="dash-editbar-long"> layout</span>
          </button>
          <button
            type="button"
            className="button button-secondary"
            disabled={saving}
            onClick={cancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className="button button-primary"
            disabled={saving}
            onClick={() => {
              if (viewAs !== null) {
                setOwnLayout(draft);
                return dispatch({ type: "kept" });
              }
              dispatch({ type: "done", after: fetcher.data });
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
            style={
              {
                "--span-3": wide[index].span,
                "--order-3": wide[index].order,
                "--span-2": medium[index].span,
                "--order-2": medium[index].order,
              } as CSSProperties
            }
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
                    dispatch({ type: "dragStart", card });
                  }}
                >
                  ⠿
                </button>
                <span className="dash-edit-name">
                  <DashboardIcon name={CARDS[card].icon} />
                  {CARDS[card].title}
                </span>
                {CARDS[card].settings && viewAs === null ? (
                  <button
                    type="button"
                    className="button button-secondary dash-card-settings"
                    aria-label={`${CARDS[card].title} settings`}
                    disabled={saving}
                    onClick={() => dispatch({ type: "openSettings", card })}
                  >
                    <DashboardIcon name="gear" />
                    <span className="dash-card-settings-label">Settings</span>
                  </button>
                ) : null}
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
                  className="dash-icon-button dash-remove"
                  aria-label={`Remove ${CARDS[card].title}`}
                  title="Remove card"
                  disabled={saving}
                  onClick={() =>
                    change(
                      cards.filter((item) => item !== card),
                      `Removed ${CARDS[card].title}. Add it back from Add a card.`,
                    )
                  }
                >
                  <DashboardIcon name="trash" />
                </button>
              </div>
            ) : null}
            {cardBody(card, reads, editing, context)}
          </section>
        ))}
      </div>
      {cards.length === 0 ? (
        <div className="dash-empty">
          <b>Your dashboard is empty</b>
          <button
            type="button"
            className="button button-primary"
            onClick={() => dispatch({ type: "openAdd" })}
          >
            <DashboardIcon name="plus" /> Add a card
          </button>
        </div>
      ) : null}
      {adding && editing ? (
        <AddSheet
          cards={addableCards(cards)}
          saving={saving}
          onClose={() => dispatch({ type: "closeAdd" })}
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
          onClose={() => dispatch({ type: "closeSettings" })}
        />
      ) : null}
      {firstVisit ? (
        <FirstVisit
          saving={saving}
          onPick={(choice) => {
            if (choice === "default") return save({ ...settings, layout: "default" });
            save({ ...settings, layout: [] });
            dispatch({ type: "openAdd" });
          }}
        />
      ) : null}
    </>
  );
}
