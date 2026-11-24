import { useEffect, useRef, type ReactNode } from "react";
import { useLocation } from "react-router";

export function SiteMenu({
  current,
  children,
}: {
  current: boolean;
  children: ReactNode;
}) {
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
      // The dimmed page behind the sheet belongs to the disclosure itself.
      const outside = !menu?.contains(event.target as Node) || event.target === menu;
      if (menu?.open && outside) menu.open = false;
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
      <summary
        ref={toggleRef}
        className="bottom-tab site-menu-toggle"
        aria-current={current ? "page" : undefined}
      >
        <svg
          width="22"
          height="22"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3.4"
          strokeLinecap="round"
          aria-hidden="true"
          focusable="false"
        >
          <path d="M5 12h.01M12 12h.01M19 12h.01" />
        </svg>
        More
      </summary>
      <div
        className="site-menu-panel"
        onClick={(event) => {
          if ((event.target as Element).closest("a")) close();
        }}
      >
        {children}
      </div>
    </details>
  );
}
