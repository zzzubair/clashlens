import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useFetcher } from "react-router";

import type { LinkedPlayerCard } from "../lib/account-contracts";
import type {
  CardData,
  CardId,
  DashboardLayout,
  DashboardTab,
  LegendsHeld,
  OpponentRow,
  PlacedCard,
  PlayerDay,
  RankRange,
} from "../lib/dashboard";
import {
  CARD_IDS,
  CARD_SIZE_LABELS,
  CARDS,
  DASHBOARD_TABS,
  serializeLayout,
} from "../lib/dashboard";
import { LOOKUP_MESSAGES } from "../lib/player-lookup-text";
import type { DashboardActionData } from "../routes/dashboard";
import { DashboardIcon } from "./DashboardIcon";
import { LegendClock } from "./LegendClock";
import { LegendDayCard } from "./LegendDayCard";
import { OpponentsCard } from "./OpponentsCard";

const DATA_TAGS: Record<CardData, string | null> = {
  ready: null,
  "new-read": "new read",
  estimate: "≈ estimate",
  "new-data": "needs new data",
  "not-ready": "not ready",
};

const TAB_LABELS = Object.fromEntries(
  DASHBOARD_TABS.map((item) => [item.id, item.label]),
) as Record<DashboardTab, string>;

function playerName(player: LinkedPlayerCard): string {
  return player.name ?? player.tag;
}

function deviceTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function timeZoneOptions(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return ["UTC"];
  }
}

/** "updated 3 min ago", or nothing until the browser knows the time. */
function updatedAgo(observedAtMs: number | null, nowMs: number | null): string | null {
  if (observedAtMs === null || nowMs === null) return null;
  const minutes = Math.max(0, Math.floor((nowMs - observedAtMs) / 60_000));
  if (minutes < 1) return "updated just now";
  if (minutes < 120) return `updated ${minutes} min ago`;
  return `updated ${Math.floor(minutes / 60)} h ago`;
}

/** A card with no content yet: its title, size and what it will show. */
export function PlaceholderCard({
  placed,
  tools,
  pinnedTo,
}: {
  placed: PlacedCard;
  tools?: ReactNode;
  pinnedTo?: string | null;
}) {
  const definition = CARDS[placed.card];
  return (
    <CardFrame placed={placed} tools={tools} titleExtra={pinnedTo}>
      <div className="dash-placeholder">
        <DashboardIcon name={definition.icon} />
        <p className="dash-placeholder-label">
          Placeholder · {CARD_SIZE_LABELS[definition.size]}
        </p>
        <p className="dash-placeholder-what">{definition.what}</p>
        {DATA_TAGS[definition.data] ? (
          <span className="dash-tag">{DATA_TAGS[definition.data]}</span>
        ) : null}
      </div>
    </CardFrame>
  );
}

function CardFrame({
  placed,
  tools,
  titleExtra,
  meta,
  footer,
  children,
}: {
  placed: PlacedCard;
  tools?: ReactNode;
  /** Shown after the title, such as the player's name; cut short, in full on hover. */
  titleExtra?: string | null;
  /** Top-right corner, such as "updated 2 min ago". */
  meta?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  const definition = CARDS[placed.card];
  return (
    <section
      className={`dash-card dash-card-${definition.size}${tools ? " dash-card-editing" : ""}`}
      aria-label={definition.title}
      data-card={placed.card}
    >
      {tools}
      <header className="dash-card-head">
        <h2>
          {definition.title}
          {titleExtra ? (
            <span className="dash-card-extra">
              {" · "}
              <bdi className="dash-name" title={titleExtra}>
                {titleExtra}
              </bdi>
            </span>
          ) : null}
        </h2>
        {meta ? <span className="dash-card-meta">{meta}</span> : null}
      </header>
      {children}
      {footer ? <p className="dash-card-foot">{footer}</p> : null}
    </section>
  );
}

/** What one placed card shows for one player. */
interface CardContext {
  player: LinkedPlayerCard;
  pinned: boolean;
  day: PlayerDay | null;
  range: RankRange | null;
  opponents: OpponentRow[] | null;
  legends: LegendsHeld | null;
  nowMs: number | null;
  timeZone: string;
}

interface CardContent {
  body: ReactNode;
  titleExtra?: string | null;
  meta?: ReactNode;
  footer?: ReactNode;
}

/**
 * The cards that have content. A card missing here shows its placeholder,
 * so a new card is one entry here plus its component.
 */
const CARD_CONTENT: Partial<
  Record<CardId, (context: CardContext) => CardContent | null>
> = {
  legendday: (context) => ({
    titleExtra: playerName(context.player),
    meta: [
      context.day?.dayNumber
        ? `Day ${context.day.dayNumber} of ${context.day.dayCount ?? 28}`
        : null,
      updatedAgo(context.day?.observedAtMs ?? null, context.nowMs),
    ]
      .filter(Boolean)
      .join(" · "),
    body: (
      <LegendDayCard player={context.player} day={context.day} range={context.range} />
    ),
  }),
  clock: (context) => ({
    titleExtra: context.pinned ? playerName(context.player) : null,
    meta: updatedAgo(context.day?.battlesObservedAtMs ?? null, context.nowMs),
    footer: `Times in your zone: ${context.timeZone}`,
    body: (
      <LegendClock
        battles={context.day?.battles ?? []}
        nowMs={context.nowMs}
        timeZone={context.timeZone}
      />
    ),
  }),
  opponents: (context) => {
    if (context.opponents === null) return null;
    const oldest = context.opponents.reduce<number | null>(
      (min, row) =>
        row.observedAtMs === null
          ? min
          : min === null
            ? row.observedAtMs
            : Math.min(min, row.observedAtMs),
      null,
    );
    return {
      titleExtra: `${context.pinned ? `${playerName(context.player)} · ` : ""}today · ${context.opponents.length} of 8`,
      meta: updatedAgo(oldest, context.nowMs),
      body: (
        <OpponentsCard
          rows={context.opponents}
          legends={context.legends}
          timeZone={context.timeZone}
        />
      ),
    };
  },
};

function CardTools({
  placed,
  index,
  count,
  players,
  onChange,
}: {
  placed: PlacedCard;
  index: number;
  count: number;
  players: LinkedPlayerCard[];
  onChange: (change: { move?: -1 | 1; player?: string | null; remove?: true }) => void;
}) {
  const definition = CARDS[placed.card];
  return (
    <div className="dash-card-tools">
      <button
        type="button"
        className="dash-icon-button"
        aria-label={`Move ${definition.title} up`}
        disabled={index === 0}
        onClick={() => onChange({ move: -1 })}
      >
        <DashboardIcon name="up" />
      </button>
      <button
        type="button"
        className="dash-icon-button"
        aria-label={`Move ${definition.title} down`}
        disabled={index === count - 1}
        onClick={() => onChange({ move: 1 })}
      >
        <DashboardIcon name="down" />
      </button>
      {definition.perPlayer && players.length > 0 ? (
        <select
          aria-label={`Pin ${definition.title} to one account`}
          value={placed.player ?? ""}
          onChange={(event) => onChange({ player: event.currentTarget.value || null })}
        >
          <option value="">Not pinned</option>
          {players.map((player) => (
            <option key={player.tag} value={player.tag}>
              Pin to {playerName(player)}
            </option>
          ))}
        </select>
      ) : null}
      <button
        type="button"
        className="dash-icon-button dash-remove"
        aria-label={`Remove ${definition.title}`}
        onClick={() => onChange({ remove: true })}
      >
        <DashboardIcon name="x" />
      </button>
    </div>
  );
}

function CardPicker({
  tab,
  placed,
  onAdd,
  onClose,
}: {
  tab: DashboardTab;
  placed: PlacedCard[];
  onAdd: (card: CardId) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const shown = CARD_IDS.filter((id) => CARDS[id].tab === tab);
  return (
    <dialog
      ref={dialog}
      className="dash-picker"
      aria-labelledby="dash-picker-title"
      onClose={onClose}
    >
      <header className="dash-picker-head">
        <h2 id="dash-picker-title">
          <DashboardIcon name="plus" /> Add a card to {TAB_LABELS[tab]}
        </h2>
        <button
          type="button"
          className="dash-icon-button"
          aria-label="Close"
          onClick={() => dialog.current?.close()}
        >
          <DashboardIcon name="x" />
        </button>
      </header>
      <ul className="dash-picker-list">
        {shown.map((id) => {
          const definition = CARDS[id];
          const onTab = placed.some((card) => card.card === id);
          return (
            <li key={id} className="dash-picker-card">
              <div className="dash-picker-preview" aria-hidden="true">
                <DashboardIcon name={definition.icon} />
              </div>
              <h3>
                <DashboardIcon name={definition.icon} />
                {definition.title}
              </h3>
              <p>{definition.what}</p>
              <p className="dash-picker-tags">
                <span className="dash-size-chip">
                  {CARD_SIZE_LABELS[definition.size]}
                </span>
                {DATA_TAGS[definition.data] ? (
                  <span className="dash-tag">{DATA_TAGS[definition.data]}</span>
                ) : null}
                {definition.offByDefault ? (
                  <span className="dash-tag">off by default</span>
                ) : null}
              </p>
              {onTab ? (
                <p className="dash-picker-on">
                  <DashboardIcon name="check" /> on this tab
                </p>
              ) : null}
              <button
                type="button"
                className="button button-primary"
                onClick={() => {
                  onAdd(id);
                  dialog.current?.close();
                }}
              >
                <DashboardIcon name="plus" /> Add to {TAB_LABELS[tab]}
              </button>
            </li>
          );
        })}
      </ul>
    </dialog>
  );
}

function LinkPlayerPrompt() {
  return (
    <section
      className="dash-card dash-card-xl dash-link"
      aria-labelledby="dash-link-title"
    >
      <header className="dash-card-head">
        <h2 id="dash-link-title">
          <DashboardIcon name="link" /> Link your Clash player
        </h2>
      </header>
      <ol className="dash-link-steps">
        <li>In Clash of Clans, open Settings → More Settings.</li>
        <li>Copy your player tag and API token.</li>
        <li>Paste both on the next page.</li>
      </ol>
      <Link className="button button-primary" to="/account/verify-player">
        Link player
      </Link>
    </section>
  );
}

function moveCard(cards: PlacedCard[], index: number, step: -1 | 1): PlacedCard[] {
  const next = [...cards];
  const target = index + step;
  if (target < 0 || target >= next.length) return cards;
  [next[index], next[target]] = [next[target] as PlacedCard, next[index] as PlacedCard];
  return next;
}

/**
 * The tab row, the Customise flow and the card grid for one dashboard tab.
 * Customise edits a draft of every tab; Done saves it to the account.
 */
export function DashboardGrid({
  tab,
  layout,
  players,
  selected,
  days,
  ranges,
  opponents,
  legends,
  dayEndsMs,
  idempotencyKey,
  renderTabs,
  noPlayers,
}: {
  tab: DashboardTab;
  layout: DashboardLayout;
  players: LinkedPlayerCard[];
  selected: LinkedPlayerCard | null;
  days: Record<string, PlayerDay>;
  ranges: Record<string, RankRange>;
  opponents: Record<string, OpponentRow[]>;
  legends: LegendsHeld | null;
  dayEndsMs: number;
  idempotencyKey: string;
  renderTabs: (meta: ReactNode) => ReactNode;
  noPlayers: boolean;
}) {
  const fetcher = useFetcher<DashboardActionData>();
  const [draft, setDraft] = useState<DashboardLayout | null>(null);
  const [picking, setPicking] = useState(false);
  const [nowMs, setNowMs] = useState<number | null>(null);
  const [device, setDevice] = useState<string | null>(null);

  useEffect(() => {
    setDevice(deviceTimeZone());
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (fetcher.state === "idle" && fetcher.data?.saved) setDraft(null);
  }, [fetcher.state, fetcher.data]);

  const editing = draft !== null;
  const shown = draft ?? layout;
  const cards = shown.tabs[tab];
  const saving = fetcher.state !== "idle";
  const timeZone = shown.timeZone === "auto" ? (device ?? "UTC") : shown.timeZone;
  const dayOver = nowMs !== null && nowMs >= dayEndsMs;
  const notTracking = [
    selected,
    ...cards.map((card) => players.find((player) => player.tag === card.player)),
  ].filter(
    (player, index, all): player is LinkedPlayerCard =>
      !!player &&
      player.state !== "tracking" &&
      all.findIndex((other) => other?.tag === player.tag) === index,
  );

  const editDraft = (change: (current: DashboardLayout) => DashboardLayout) => {
    if (!saving) setDraft((current) => (current ? change(current) : current));
  };
  const updateTab = (next: PlacedCard[]) =>
    editDraft((current) => ({ ...current, tabs: { ...current.tabs, [tab]: next } }));

  const meta =
    !editing && !noPlayers ? (
      <div className="dash-meta">
        <button
          type="button"
          className="button button-primary dash-customise"
          onClick={() => setDraft(structuredClone(layout))}
        >
          <DashboardIcon name="grid" /> Customise
        </button>
      </div>
    ) : null;

  if (noPlayers) {
    return (
      <>
        {renderTabs(meta)}
        <div className="dash-grid">
          <LinkPlayerPrompt />
        </div>
      </>
    );
  }

  return (
    <>
      {renderTabs(meta)}
      {editing ? (
        <div className="dash-editbar" role="region" aria-label="Customise">
          <span className="dash-editbar-title">
            <DashboardIcon name="grid" /> Customising {TAB_LABELS[tab]}
          </span>
          <button
            type="button"
            className="button button-primary dash-add"
            disabled={saving}
            onClick={() => setPicking(true)}
          >
            <DashboardIcon name="plus" /> Add a card
          </button>
          <label className="dash-timezone">
            <DashboardIcon name="globe" />
            <span className="sr-only">Time zone</span>
            <select
              value={shown.timeZone}
              onChange={(event) => {
                const value = event.currentTarget.value;
                editDraft((current) => ({ ...current, timeZone: value }));
              }}
            >
              <option value="auto">Device time ({device ?? "auto"})</option>
              {timeZoneOptions().map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="button button-secondary"
            disabled={saving}
            onClick={() => setDraft(null)}
          >
            Cancel
          </button>
          <button
            type="button"
            className="button button-secondary dash-done"
            disabled={saving}
            onClick={() =>
              fetcher.submit(
                {
                  idempotencyKey: fetcher.data?.idempotencyKey ?? idempotencyKey,
                  layout: JSON.stringify(serializeLayout(shown)),
                },
                { method: "post" },
              )
            }
          >
            <DashboardIcon name="check" /> {saving ? "Saving…" : "Done"}
          </button>
          {fetcher.state === "idle" && fetcher.data?.error ? (
            <p className="dash-editbar-error" role="alert">
              {fetcher.data.error}
            </p>
          ) : null}
        </div>
      ) : null}
      {notTracking.map((player) => (
        <p key={player.tag} className="dash-banner" role="status">
          {player.state === "not_in_legend" ? (
            <>
              <DashboardIcon name="shieldOff" />
              <b>{playerName(player)} is not in Legends</b>
            </>
          ) : (
            <>
              <DashboardIcon name="info" />
              <b>{playerName(player)}</b> {LOOKUP_MESSAGES[player.state]}
            </>
          )}
        </p>
      ))}
      <div className="dash-grid">
        {cards.map((placed, index) => {
          const definition = CARDS[placed.card];
          const pinned = placed.player
            ? (players.find((player) => player.tag === placed.player) ?? null)
            : null;
          const player = definition.perPlayer ? (pinned ?? selected) : null;
          if (!editing && player && player.state !== "tracking") return null;
          const tools = editing ? (
            <CardTools
              placed={placed}
              index={index}
              count={cards.length}
              players={players}
              onChange={(change) => {
                if (change.remove)
                  return updateTab(cards.filter((_, at) => at !== index));
                if (change.move) return updateTab(moveCard(cards, index, change.move));
                updateTab(
                  cards.map((card, at) =>
                    at === index
                      ? {
                          ...card,
                          ...("player" in change
                            ? { player: change.player ?? null }
                            : {}),
                        }
                      : card,
                  ),
                );
              }}
            />
          ) : undefined;
          const pinnedTo = pinned ? playerName(pinned) : null;
          const content =
            player && !dayOver
              ? CARD_CONTENT[placed.card]?.({
                  player,
                  pinned: pinned !== null,
                  day: days[player.tag] ?? null,
                  range: ranges[player.tag] ?? null,
                  opponents: opponents[player.tag] ?? null,
                  legends,
                  nowMs,
                  timeZone,
                })
              : null;
          if (content) {
            return (
              <CardFrame
                key={index}
                placed={placed}
                tools={tools}
                titleExtra={content.titleExtra}
                meta={content.meta}
                footer={content.footer}
              >
                {content.body}
              </CardFrame>
            );
          }
          return (
            <PlaceholderCard
              key={index}
              placed={placed}
              tools={tools}
              pinnedTo={pinnedTo}
            />
          );
        })}
      </div>
      {cards.length === 0 ? (
        <div className="dash-empty dash-empty-tab">
          <DashboardIcon name="grid" />
          <b>No cards on this tab</b>
          {!editing ? (
            <button
              type="button"
              className="button button-primary"
              onClick={() => {
                setDraft(structuredClone(layout));
                setPicking(true);
              }}
            >
              <DashboardIcon name="plus" /> Add a card
            </button>
          ) : null}
        </div>
      ) : null}
      {picking && draft ? (
        <CardPicker
          tab={tab}
          placed={cards}
          onClose={() => setPicking(false)}
          onAdd={(card) => updateTab([...cards, { card, player: null }])}
        />
      ) : null}
    </>
  );
}
