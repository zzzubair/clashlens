import type { ReactNode } from "react";

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
