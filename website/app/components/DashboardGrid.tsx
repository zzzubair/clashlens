import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useFetcher } from "react-router";

import type { LinkedPlayerCard } from "../lib/account-contracts";
import type {
  CardData,
  CardId,
  CardSize,
  DashboardLayout,
  DashboardTab,
  PlacedCard,
  PlayerDay,
} from "../lib/dashboard";
import {
  CARD_IDS,
  CARD_SIZES,
  CARDS,
  DASHBOARD_TABS,
  defaultTab,
  serializeLayout,
} from "../lib/dashboard";
import type { DashboardActionData } from "../routes/dashboard";
import { DashboardIcon } from "./DashboardIcon";
import { LegendClock, timeZoneLabel } from "./LegendClock";

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
    <CardFrame placed={placed} tools={tools} pinnedTo={pinnedTo}>
      <div className="dash-placeholder">
        <DashboardIcon name={definition.icon} />
        <p className="dash-placeholder-label">
          Placeholder · {placed.size.toUpperCase()}
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
  pinnedTo,
  children,
}: {
  placed: PlacedCard;
  tools?: ReactNode;
  pinnedTo?: string | null;
  children: ReactNode;
}) {
  const definition = CARDS[placed.card];
  return (
    <section
      className={`dash-card dash-card-${placed.size}${tools ? " dash-card-editing" : ""}`}
      aria-label={definition.title}
      data-card={placed.card}
    >
      {tools}
      <header className="dash-card-head">
        <h2>
          <DashboardIcon name={definition.icon} />
          {definition.title}
        </h2>
        {pinnedTo ? <span className="dash-pin">{pinnedTo}</span> : null}
      </header>
      {children}
    </section>
  );
}

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
  onChange: (change: {
    move?: -1 | 1;
    size?: CardSize;
    player?: string | null;
    remove?: true;
  }) => void;
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
      <div className="dash-sizes" role="group" aria-label={`${definition.title} size`}>
        {CARD_SIZES.map((size) => (
          <button
            key={size}
            type="button"
            aria-pressed={placed.size === size}
            disabled={!definition.sizes.includes(size)}
            onClick={() => onChange({ size })}
          >
            {size.toUpperCase()}
          </button>
        ))}
      </div>
      {definition.perPlayer && players.length > 0 ? (
        <select
          aria-label={`Which player ${definition.title} shows`}
          value={placed.player ?? ""}
          onChange={(event) => onChange({ player: event.currentTarget.value || null })}
        >
          <option value="">Follows switcher</option>
          {players.map((player) => (
            <option key={player.tag} value={player.tag}>
              Always {playerName(player)}
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
  const [filter, setFilter] = useState<DashboardTab | "all">("all");
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const shown = CARD_IDS.filter((id) => filter === "all" || CARDS[id].tab === filter);
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
      <div className="dash-picker-filters" role="group" aria-label="Show cards from">
        {(["all", ...DASHBOARD_TABS.map((item) => item.id)] as const).map((id) => (
          <button
            key={id}
            type="button"
            aria-pressed={filter === id}
            onClick={() => setFilter(id)}
          >
            {id === "all" ? "All" : TAB_LABELS[id]}
          </button>
        ))}
      </div>
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
                {definition.sizes.map((size) => (
                  <span key={size} className="dash-size-chip">
                    {size.toUpperCase()}
                  </span>
                ))}
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
  idempotencyKey,
  renderTabs,
  dayLabel,
  noPlayers,
}: {
  tab: DashboardTab;
  layout: DashboardLayout;
  players: LinkedPlayerCard[];
  selected: LinkedPlayerCard | null;
  days: Record<string, PlayerDay>;
  idempotencyKey: string;
  renderTabs: (meta: ReactNode) => ReactNode;
  dayLabel: string | null;
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
  const selectedInLegends = selected?.state === "tracking";

  const updateTab = (next: PlacedCard[]) =>
    setDraft((current) =>
      current ? { ...current, tabs: { ...current.tabs, [tab]: next } } : current,
    );

  const meta = (
    <div className="dash-meta">
      {dayLabel ? (
        <span>
          <DashboardIcon name="cal" /> {dayLabel}
        </span>
      ) : null}
      {nowMs !== null ? (
        <span>
          <DashboardIcon name="globe" /> times in {timeZoneLabel(timeZone, nowMs)}
        </span>
      ) : null}
      {!editing && !noPlayers ? (
        <button
          type="button"
          className="button button-primary dash-customise"
          onClick={() => setDraft(structuredClone(layout))}
        >
          <DashboardIcon name="grid" /> Customise
        </button>
      ) : null}
    </div>
  );

  if (noPlayers) {
    return (
      <>
        {renderTabs(meta)}
        <div className="dash-grid">
          <LinkPlayerPrompt />
          <PlaceholderCard placed={{ card: "cutoffs", size: "l", player: null }} />
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
                setDraft((current) =>
                  current ? { ...current, timeZone: value } : current,
                );
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
            onClick={() => updateTab(defaultTab(tab))}
          >
            Reset
          </button>
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
      {selected && !selectedInLegends && !editing ? (
        <p className="dash-banner" role="status">
          <DashboardIcon name="shieldOff" />
          <b>{playerName(selected)} is not in Legends</b>
        </p>
      ) : null}
      <div className="dash-grid">
        {cards.map((placed, index) => {
          const definition = CARDS[placed.card];
          const pinned = placed.player
            ? (players.find((player) => player.tag === placed.player) ?? null)
            : null;
          const player = definition.perPlayer ? (pinned ?? selected) : null;
          if (!editing && definition.perPlayer && !pinned && !selectedInLegends) {
            return null;
          }
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
                          ...(change.size ? { size: change.size } : {}),
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
          if (placed.card === "clock" && player?.state === "tracking") {
            return (
              <CardFrame key={index} placed={placed} tools={tools} pinnedTo={pinnedTo}>
                <LegendClock
                  player={player}
                  day={days[player.tag] ?? null}
                  size={placed.size}
                  nowMs={nowMs}
                  timeZone={timeZone}
                />
              </CardFrame>
            );
          }
          if (definition.perPlayer && player && player.state !== "tracking") {
            return (
              <CardFrame key={index} placed={placed} tools={tools} pinnedTo={pinnedTo}>
                <p className="dash-empty">
                  <DashboardIcon name="shieldOff" /> Not in Legends
                </p>
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
          onAdd={(card) =>
            updateTab([...cards, { card, size: CARDS[card].defaultSize, player: null }])
          }
        />
      ) : null}
    </>
  );
}
