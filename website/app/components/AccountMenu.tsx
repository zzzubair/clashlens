import { useEffect, useRef, useState } from "react";
import { Form, Link } from "react-router";

/**
 * The signed-in account name in the header. Clicking it opens a small list
 * with Account and Log out; outside clicks, Escape and tabbing away close it.
 */
export function AccountMenu({
  label,
  accountPath,
  logoutIdempotencyKey,
}: {
  label: string;
  accountPath: string;
  logoutIdempotencyKey: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!expanded) return;
    function handlePointerDown(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setExpanded(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [expanded]);

  return (
    <div
      ref={rootRef}
      className="account-menu"
      onKeyDown={(event) => {
        if (expanded && event.key === "Escape") {
          event.preventDefault();
          setExpanded(false);
          toggleRef.current?.focus();
        }
      }}
      onBlur={(event) => {
        if (
          event.relatedTarget !== null &&
          !event.currentTarget.contains(event.relatedTarget)
        ) {
          setExpanded(false);
        }
      }}
    >
      <button
        ref={toggleRef}
        type="button"
        className="nav-link nav-account"
        aria-expanded={expanded}
        aria-controls="account-menu-panel"
        onClick={() => setExpanded(!expanded)}
      >
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          aria-hidden="true"
          focusable="false"
        >
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21v-2a8 8 0 0 1 16 0v2" />
        </svg>
        <span className="nav-account-name">{label}</span>
        <svg
          className="account-menu-chevron"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          focusable="false"
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      <ul id="account-menu-panel" className="account-menu-panel" hidden={!expanded}>
        <li>
          <Link to={accountPath} onClick={() => setExpanded(false)}>
            Account
          </Link>
        </li>
        <li>
          <Form method="post" action="/logout">
            <input type="hidden" name="idempotencyKey" value={logoutIdempotencyKey} />
            <button type="submit">Log out</button>
          </Form>
        </li>
      </ul>
    </div>
  );
}
