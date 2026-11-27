import { useLayoutEffect, useRef } from "react";

/** A board's rows slide from their old place to their new one when the order
 * changes (Live to At Reset, Live to Season), so a Clasher sees who moved.
 * Each row's last place is kept against its element, which React keeps for
 * the same player, and is measured only when the board renders, so nothing
 * polls in the background. Rows that arrive, as on a new page, just appear.
 * Nothing moves under reduced motion. */
export function useRowMoves<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const places = useRef(new WeakMap<Element, number>());
  useLayoutEffect(() => {
    const list = ref.current;
    if (!list) return;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    for (const row of list.children) {
      const top = (row as HTMLElement).offsetTop;
      const before = places.current.get(row);
      places.current.set(row, top);
      // A move of a few pixels is a row height change, not a new place.
      if (still || before === undefined || Math.abs(before - top) < 4) continue;
      row.animate(
        [{ transform: `translateY(${before - top}px)` }, { transform: "none" }],
        { duration: 320, easing: "cubic-bezier(0.2, 0, 0, 1)" },
      );
    }
  });
  return ref;
}
