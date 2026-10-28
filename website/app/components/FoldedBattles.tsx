import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";

const noChange = () => () => {};
// One object: React sets an element's HTML again whenever this prop changes.
const serverRows = { __html: "" };

type Battles = { offenseEvents: unknown[]; defenseEvents: unknown[] };
const battlesOf = (day: Battles) =>
  JSON.stringify([day.offenseEvents, day.defenseEvents]);

/**
 * A Legend day's battle columns. The server always sends every battle, so
 * search, print and browsing without scripts see them all. On the page React
 * first takes over, a folded day keeps the server's rows as they are instead,
 * which on a full Season is most of the page, and renders them for real once
 * the day opens or a refresh changes its battles.
 */
export function FoldedBattles({
  open,
  day,
  children,
}: {
  open: boolean;
  day: Battles;
  children: ReactNode;
}) {
  const hydrating = useSyncExternalStore(
    noChange,
    () => false,
    () => true,
  );
  const [kept, setKept] = useState(
    () => typeof document !== "undefined" && hydrating && !open,
  );
  // The day the server's rows show, or a later copy with the same battles.
  const [drawn, setDrawn] = useState(day);
  if (kept && (open || (day !== drawn && battlesOf(day) !== battlesOf(drawn)))) {
    setKept(false);
  } else if (kept && day !== drawn) setDrawn(day);
  const columns = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const details = columns.current?.closest("details");
    if (!kept || !details) return;
    const show = () => setKept(false);
    // A day open already, or holding the battle a link points to, is drawn now.
    if (details.open || columns.current?.querySelector(":target")) return show();
    details.addEventListener("toggle", show);
    return () => details.removeEventListener("toggle", show);
  }, [kept]);
  return kept ? (
    // React leaves an element's own HTML alone while it hydrates, so the rows stay.
    <div
      ref={columns}
      className="battle-sides"
      dangerouslySetInnerHTML={serverRows}
      suppressHydrationWarning
    />
  ) : (
    <div ref={columns} className="battle-sides">
      {children}
    </div>
  );
}
