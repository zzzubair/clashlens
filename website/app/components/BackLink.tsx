import { useEffect, useState } from "react";
import { Link, useLocation, useMatches, type UIMatch } from "react-router";

/** Where Back leads: the page this one sits under, and its name. */
export interface BackTarget {
  to: string;
  label: string;
}

/**
 * A route handle for a page that sits under another page. Pages reached
 * from the header have no handle and no Back. A parent named by the page's
 * own data, such as a crew, is worked out from its route match.
 */
export interface BackHandle {
  back: BackTarget | ((match: UIMatch) => BackTarget | null);
}

/**
 * Link state for a list's player links, so the player page's Back returns
 * to this list, on the same view, by a normal link.
 */
export function useBackState(label: string): { back: BackTarget } {
  const location = useLocation();
  return { back: { to: `${location.pathname}${location.search}`, label } };
}

function stateBack(state: unknown): BackTarget | null {
  const back = (state as { back?: Partial<BackTarget> } | null)?.back;
  return typeof back?.to === "string" &&
    typeof back.label === "string" &&
    back.to.startsWith("/") &&
    !back.to.startsWith("//")
    ? { to: back.to, label: back.label }
    : null;
}

/**
 * The one Back on the site: an arrow and the parent page's name, always a
 * normal link to that page, never a step through browser history. A page
 * with a Back handle always shows it; a page opened from a list shows Back
 * to that list, and its own view links carry that along.
 */
export function BackLink() {
  const location = useLocation();
  const handle = useMatches()
    .map((match) => {
      const back = (match.handle as Partial<BackHandle> | undefined)?.back;
      return typeof back === "function" ? back(match) : back;
    })
    .reverse()
    .find(Boolean);
  // Link state only exists in the browser, so it waits for the first render there.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);
  const back = handle ?? (hydrated ? stateBack(location.state) : null);
  if (!back) return null;
  return (
    <div className="page-back">
      <Link className="back-link" to={back.to}>
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M15 5l-7 7 7 7" />
        </svg>
        <span className="sr-only">Back to </span>
        <span>{back.label}</span>
      </Link>
    </div>
  );
}
