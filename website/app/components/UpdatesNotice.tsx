import type { UpdateStatus } from "../lib/contracts";
import { LocalTimestamp, formatAge } from "./Provenance";

/** One quiet site-wide line; it states a cause only when the data shows one. */
export function UpdatesNotice({ status }: { status: UpdateStatus }) {
  const ago = (value: string) =>
    `${formatAge(Math.max(0, (Date.parse(status.checkedAt) - Date.parse(value)) / 1000))} ago`;
  return (
    <div className="status-banner status-banner-warning updates-notice" role="status">
      <p>
        <strong>Updates are delayed.</strong>{" "}
        {status.lastCollectedAt ? (
          <>
            No new data has arrived from the Clash of Clans API since{" "}
            <LocalTimestamp value={status.lastCollectedAt} /> (
            {ago(status.lastCollectedAt)}).{" "}
          </>
        ) : null}
        {status.oldestWaitingAt ? (
          <>
            New data is waiting to be processed; the oldest is from{" "}
            <LocalTimestamp value={status.oldestWaitingAt} /> (
            {ago(status.oldestWaitingAt)}).{" "}
          </>
        ) : null}
        Saved values stay on the page with the time they were last updated.
      </p>
    </div>
  );
}
