import type { UpdateStatus } from "../lib/contracts";
import { LocalTimestamp, formatAge, useCurrentTime } from "./Provenance";

/** One quiet site-wide line; it states a cause only when the data shows one. */
export function UpdatesNotice({ status }: { status: UpdateStatus }) {
  const now = useCurrentTime(status.checkedAt);
  const ago = (value: string) =>
    `${formatAge(Math.max(0, (now - Date.parse(value)) / 1000))} ago`;
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
        {status.oldestWaitingSavedAt ? (
          <>
            New data is waiting to be processed; the oldest waiting data was saved at{" "}
            <LocalTimestamp value={status.oldestWaitingSavedAt} /> (
            {ago(status.oldestWaitingSavedAt)}).{" "}
          </>
        ) : null}
        Saved values stay on the page with the time they were last updated.
      </p>
    </div>
  );
}
