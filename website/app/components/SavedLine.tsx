import type { ReactNode } from "react";

/**
 * The one way a page says a save worked: one short line at the top of the
 * page, such as "Display name saved.". A save that failed says so in the same
 * place, in the warning colour.
 */
export function SavedLine({
  children,
  failed = false,
}: {
  children: ReactNode;
  failed?: boolean;
}) {
  return failed ? (
    <p className="status-banner status-banner-warning saved-line" role="alert">
      {children}
    </p>
  ) : (
    <p className="status-banner status-banner-success saved-line" role="status">
      {children}
    </p>
  );
}
