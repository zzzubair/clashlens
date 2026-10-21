import type { RefreshStatus, RefreshWork } from "../lib/contracts";

/** Whether a player's Refresh is still running and has not failed. */
export function refreshInProgress(
  status: RefreshStatus | RefreshWork | null,
  failed: boolean,
): boolean {
  return !failed && (status?.state === "queued" || status?.state === "running");
}

/**
 * A player Refresh's status beside its button. The button and the update time
 * show a refresh in place, so only screen readers hear "Refreshing…" and
 * "Updated."; only a failure is written out, and the page below never moves.
 */
export function RefreshProgress({
  status,
  failed,
}: {
  status: RefreshStatus | RefreshWork;
  failed: boolean;
}) {
  const quiet = status.state === "complete" || refreshInProgress(status, failed);
  return (
    <section
      className="player-refresh-status"
      aria-live="polite"
      aria-label="Player refresh"
    >
      <p role="status" className={quiet ? "sr-only" : undefined}>
        {status.state === "complete"
          ? "Updated."
          : quiet
            ? "Refreshing…"
            : "Couldn't refresh right now. Showing saved results."}
      </p>
    </section>
  );
}
