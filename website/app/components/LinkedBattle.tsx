import { useEffect, useState } from "react";
import { useLocation, useRevalidator } from "react-router";

import type { RankedDaySummary } from "../lib/contracts";

// Two minutes of checks, one every five seconds.
const CHECKS = 24;
const CHECK_EVERY_MS = 5_000;

/**
 * A link to one battle, /players/%23TAG?day=YYYY-MM-DD#battle-ID, opens that
 * battle highlighted. Discord alerts send a battle as soon as it is saved, a
 * little before its Legend day is recalculated, so while the page does not
 * show the battle it reads the player again every five seconds for two minutes
 * and says so. `log` are the days of the page's Legend log and `day` is a
 * linked day shown on its own; Reset can move the battle from one to the other.
 */
export function LinkedBattle({
  log,
  day,
}: {
  log: RankedDaySummary[];
  day: RankedDaySummary | undefined;
}) {
  const location = useLocation();
  const revalidator = useRevalidator();
  // The server never sees the #battle- part, so it is read after hydration.
  const [battleId, setBattleId] = useState<string | null>(null);
  const [checksLeft, setChecksLeft] = useState(CHECKS);
  useEffect(() => setBattleId(linkedBattleId(location.hash)), [location.hash]);
  const holds = (saved: RankedDaySummary) =>
    [...saved.offenseEvents, ...saved.defenseEvents].some(
      (event) => event.battleId === battleId,
    );
  const shownIn =
    battleId === null ? null : day && holds(day) ? "day" : log.some(holds) ? "log" : null;
  const shown = shownIn !== null;
  const waiting = battleId !== null && !shown && checksLeft > 0;

  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => {
      if (revalidator.state !== "idle") return;
      setChecksLeft((left) => left - 1);
      void revalidator.revalidate();
    }, CHECK_EVERY_MS);
    return () => clearInterval(timer);
  }, [waiting, revalidator]);

  useEffect(() => {
    if (battleId === null || shownIn === null) return;
    const id = `battle-${battleId}`;
    let frame = 0;
    let highlightTimer: ReturnType<typeof setTimeout> | undefined;
    const clear = () => document.getElementById(id)?.classList.remove("battle-arrival");
    const highlight = () => {
      if (document.hidden) return;
      cancelAnimationFrame(frame);
      clearTimeout(highlightTimer);
      clear();
      // Wait for the removed highlight to render so the two-second one restarts.
      frame = requestAnimationFrame(() => {
        frame = requestAnimationFrame(() => {
          const card = document.getElementById(id);
          if (!card?.matches(".battle-row")) return;
          const details = card.closest("details");
          if (details) details.open = true;
          card.scrollIntoView({ block: "center", behavior: "instant" });
          card.classList.add("battle-arrival");
          highlightTimer = setTimeout(clear, 2000);
        });
      });
    };
    if (document.readyState === "complete") highlight();
    else window.addEventListener("load", highlight, { once: true });
    window.addEventListener("pageshow", highlight);
    document.addEventListener("visibilitychange", highlight);
    return () => {
      cancelAnimationFrame(frame);
      clearTimeout(highlightTimer);
      window.removeEventListener("load", highlight);
      window.removeEventListener("pageshow", highlight);
      document.removeEventListener("visibilitychange", highlight);
      clear();
    };
  }, [battleId, shownIn, location.key]);

  if (battleId === null || shown) return null;
  return (
    <aside className="notice" role="status">
      {waiting ? (
        "Still adding this battle to the Legend log. This page checks again every few seconds."
      ) : (
        <>
          This battle isn't on this page. A battle from the last few minutes can take a
          little longer.{" "}
          <button
            type="button"
            className="button secondary"
            onClick={() => {
              setChecksLeft(CHECKS);
              void revalidator.revalidate();
            }}
          >
            Check again
          </button>
        </>
      )}
    </aside>
  );
}

function linkedBattleId(hash: string): string | null {
  if (!hash.startsWith("#battle-")) return null;
  try {
    return decodeURIComponent(hash.slice("#battle-".length));
  } catch {
    return null;
  }
}
