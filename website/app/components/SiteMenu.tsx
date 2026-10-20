import { useEffect, useRef, type ReactNode } from "react";
import { useLocation } from "react-router";

/**
 * The top bar's Menu on narrow screens: the tabs, the theme switch and Join
 * Discord. Opening a page, clicking outside, Escape and tabbing away close
 * it. It is a native disclosure, so it also opens before or without page
 * scripts.
 */
export function SiteMenu({ children }: { children: ReactNode }) {
  const menuRef = useRef<HTMLDetailsElement>(null);
  const toggleRef = useRef<HTMLElement>(null);
  const location = useLocation();

  function close() {
    if (menuRef.current) menuRef.current.open = false;
  }

  useEffect(close, [location.pathname, location.search]);

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
      className="site-menu"
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
      <summary ref={toggleRef} className="nav-link site-menu-toggle">
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          aria-hidden="true"
          focusable="false"
        >
          <path d="M4 7h16M4 12h16M4 17h16" />
        </svg>
        <span>Menu</span>
      </summary>
      <div className="site-menu-panel">{children}</div>
    </details>
  );
}
