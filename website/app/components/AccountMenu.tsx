import { useEffect, useRef } from "react";
import { Form, Link } from "react-router";

/**
 * The signed-in account name in the header. Clicking it opens a small list
 * with Account and Log out; outside clicks, Escape and tabbing away close it.
 * It is a native disclosure, so it also opens before or without page scripts.
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
  const menuRef = useRef<HTMLDetailsElement>(null);
  const toggleRef = useRef<HTMLElement>(null);

  function close() {
    if (menuRef.current) menuRef.current.open = false;
  }

  useEffect(() => {
    function handlePointerDown(event: PointerEvent) {
      const menu = menuRef.current;
      if (menu?.open && !menu.contains(event.target as Node)) menu.open = false;
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  return (
    <details
      ref={menuRef}
      className="account-menu"
      onKeyDown={(event) => {
        if (event.currentTarget.open && event.key === "Escape") {
          event.preventDefault();
          close();
          toggleRef.current?.focus();
        }
      }}
      onBlur={(event) => {
        if (
          event.relatedTarget !== null &&
          !event.currentTarget.contains(event.relatedTarget)
        ) {
          close();
        }
      }}
    >
      <summary ref={toggleRef} className="nav-link nav-account">
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
      </summary>
      <ul className="account-menu-panel">
        <li>
          <Link to={accountPath} onClick={close}>
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
    </details>
  );
}
