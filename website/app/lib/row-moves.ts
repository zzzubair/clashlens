import { useLayoutEffect, useRef } from "react";

/** A board's rows slide from their old place to their new one when the order
 * changes (Live to At Reset, Live to Season), so a Clasher sees who moved.
 * Each row's last place in the order and on screen is kept against its
 * element, which React keeps for the same player, and is measured only when
 * the board renders, so nothing polls in the background. Only a row whose
 * place in the order changed slides: rows that arrive, as on a new page, or
 * that only shift because rows above them showed or hid, stay still, and a
 * hidden row is not measured. Nothing moves under reduced motion. */
export function useRowMoves<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const places = useRef(new WeakMap<Element, { index: number; top: number }>());
  useLayoutEffect(() => {
    const list = ref.current;
    if (!list) return;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    [...list.children].forEach((row, index) => {
      if (!(row instanceof HTMLElement) || row.offsetParent === null) {
        places.current.delete(row);
        return;
      }
      const top = row.offsetTop;
      const before = places.current.get(row);
      places.current.set(row, { index, top });
      if (still || !before || before.index === index || before.top === top) return;
      row.animate(
        [{ transform: `translateY(${before.top - top}px)` }, { transform: "none" }],
        { duration: 320, easing: "cubic-bezier(0.2, 0, 0, 1)" },
      );
    });
  });
  return ref;
}
