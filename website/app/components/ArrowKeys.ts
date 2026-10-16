import type { KeyboardEvent } from "react";

/**
 * Up and Down arrows move focus through the items matching `selector` inside
 * the element handling the key, focusing each item's link. Up from the first
 * item returns to `above`, such as the search box the items belong to.
 */
export function moveFocusWithArrows(
  event: KeyboardEvent<HTMLElement>,
  selector: string,
  above?: HTMLElement | null,
) {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const items = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(selector));
  if (items.length === 0) return;
  const current = items.findIndex((item) => item.contains(document.activeElement));
  if (current === -1 && event.key === "ArrowUp") return;
  event.preventDefault();
  const next = current + (event.key === "ArrowDown" ? 1 : -1);
  if (next < 0) {
    above?.focus();
    return;
  }
  const item = items[Math.min(next, items.length - 1)];
  const link = item.matches("a") ? item : item.querySelector("a");
  link?.focus({ preventScroll: true });
  item.scrollIntoView({ block: "nearest" });
}
