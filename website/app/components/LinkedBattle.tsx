import { useEffect, useState } from "react";
import { useLocation, useRevalidator } from "react-router";

import type { RankedDaySummary } from "../lib/contracts";

const DAY_MS = 24 * 60 * 60 * 1000;
// Two minutes of checks, one every five seconds.
const CHECKS = 24;
const CHECK_EVERY_MS = 5_000;

/**
 * A link to one battle, /players/%23TAG?day=YYYY-MM-DD#battle-ID, opens that
 * battle highlighted. Discord alerts send a battle as soon as it is saved, a
 * little before its Legend day is recalculated, so while the page does not
 * show the battle it reads the player again every five seconds for two minutes
 * and says so. `days` are the days whose battles the page shows.
 */
export function LinkedBattle({
  days,
  day,
}: {
  days: RankedDaySummary[];
  day: string | null;
}) {
  const location = useLocation();
  const revalidator = useRevalidator();
  // The server never sees the #battle- part, so it is read after hydration.
  const [battleId, setBattleId] = useState<string | null>(null);
  const [checksLeft, setChecksLeft] = useState(CHECKS);
  useEffect(() => setBattleId(linkedBattleId(location.hash)), [location.hash]);
  const shown =
    battleId !== null &&
    days.some((saved) =>
      [...saved.offenseEvents, ...saved.defenseEvents].some(
        (event) => event.battleId === battleId,
      ),
    );
  // Only a battle from today's or yesterday's Legend day can still be arriving.
  const recent = day === null || Date.parse(`${day}T05:00:00Z`) > Date.now() - 2 * DAY_MS;
  const waiting = battleId !== null && !shown && recent && checksLeft > 0;

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
    if (battleId === null || !shown) return;
    const battle = document.getElementById(`battle-${battleId}`);
    if (!battle?.matches(".battle-slot")) return;
    let frame = 0;
    let highlightTimer: ReturnType<typeof setTimeout> | undefined;
    const highlight = () => {
      if (document.hidden) return;
      cancelAnimationFrame(frame);
      clearTimeout(highlightTimer);
      battle.classList.remove("battle-arrival");
      const details = battle.closest("details");
      if (details) details.open = true;
      battle.scrollIntoView({ block: "center", behavior: "instant" });
      // Let scrolling and layout finish before the two-second highlight starts.
      frame = requestAnimationFrame(() => {
        frame = requestAnimationFrame(() => {
          battle.classList.add("battle-arrival");
          highlightTimer = setTimeout(
            () => battle.classList.remove("battle-arrival"),
            2000,
          );
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
      battle.classList.remove("battle-arrival");
    };
  }, [battleId, shown, location.key]);

  if (battleId === null || shown) return null;
  return (
    <aside className="notice" role="status">
      {waiting
        ? "Still adding this battle to the Legend log. This page checks again every few seconds."
        : "This battle isn't on this page."}
      {!waiting && recent ? (
        <>
          {" "}
          A battle from the last few minutes can take a little longer.{" "}
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
      ) : null}
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
