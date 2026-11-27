import { useAutoAnimate } from "@formkit/auto-animate/react";

/** A board's rows slide from their old place to their new one when the order
 * changes (Live to At Reset, Live to Season), so a Clasher sees who moved.
 * Rows that arrive or leave, as on a new page, just appear: 50 rows fading
 * at once would be noise. Nothing moves under reduced motion (AutoAnimate
 * leaves that check to a custom animation like this one). */
export function useRowMoves<T extends Element>() {
  const [ref] = useAutoAnimate<T>((el, action, next, previous) => {
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (still || action !== "remain" || !next || !previous) {
      return new KeyframeEffect(el, [], { duration: 0 });
    }
    const dy = previous.top - next.top;
    return new KeyframeEffect(
      el,
      [{ transform: `translateY(${dy}px)` }, { transform: "translateY(0)" }],
      { duration: dy === 0 ? 0 : 320, easing: "cubic-bezier(0.2, 0, 0, 1)" },
    );
  });
  return ref;
}
