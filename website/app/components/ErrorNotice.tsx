import type { WebsiteErrorResponse } from "../lib/contracts";

export function ErrorNotice({ error }: { error: WebsiteErrorResponse }) {
  return (
    <aside className={`notice notice-${error.error.code}`} role="alert">
      <strong>{error.error.message}</strong>
      {error.error.retryAfterSeconds ? (
        <span> Retry after about {error.error.retryAfterSeconds} seconds.</span>
      ) : null}
      {error.error.affectedDays ? (
        <span> Affected Legend days: {error.error.affectedDays.join(", ")}.</span>
      ) : null}
    </aside>
  );
}
