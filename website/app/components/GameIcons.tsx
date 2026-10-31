// The game's own symbols, drawn once and used everywhere: faceted gold stars,
// the trophy, and solid sword and shield marks for attacks and defenses.

const STAR =
  "M12 1.7 15.06 8.69 22.65 9.44 16.95 14.51 18.58 21.96 12 18.1 5.42 21.96 7.05 14.51 1.35 9.44 8.94 8.69Z";
// The lit half of each arm, so a star reads as cut metal, not a text glyph.
const STAR_LIGHT =
  "M12 12.9 8.94 8.69 12 1.7ZM12 12.9 15.06 8.69 22.65 9.44ZM12 12.9 16.95 14.51 18.58 21.96ZM12 12.9 12 18.1 5.42 21.96ZM12 12.9 7.05 14.51 1.35 9.44Z";

/** One star: gold when won, a dark hollow when missed. */
export function Star({ won }: { won: boolean }) {
  return (
    <svg
      className={won ? "game-star won" : "game-star"}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
    >
      <path className="game-star-body" d={STAR} />
      {won ? <path className="game-star-light" d={STAR_LIGHT} /> : null}
    </svg>
  );
}

/** Three stars with `count` of them won, read out as "2 stars". */
export function Stars({ count }: { count: number }) {
  const won = Math.max(0, Math.min(3, count));
  return (
    <span
      className="game-stars"
      role="img"
      aria-label={`${won} star${won === 1 ? "" : "s"}`}
    >
      {[0, 1, 2].map((index) => (
        <Star key={index} won={index < won} />
      ))}
    </span>
  );
}

/** The trophy beside trophy counts. `className` swaps the sizing class, so
 *  the older `trophy-mark` keeps the sizes pages already give it. */
export function TrophyIcon({ className = "game-trophy" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        className="game-trophy-handles"
        d="M7 4.8H4v1.5c0 2.4 1.6 4 3.8 4.1M17 4.8h3v1.5c0 2.4-1.6 4-3.8 4.1"
      />
      <path
        className="game-trophy-body"
        d="M7 2.8h10v5.6c0 3.2-2.2 5.7-5 5.7s-5-2.5-5-5.7ZM10.4 14h3.2l.7 3H9.7ZM7.4 17.2h9.2c.6 0 1 .4 1 1v2.9H6.4v-2.9c0-.6.4-1 1-1Z"
      />
      {/* The far half of the cup and base in shadow, so it reads as cast metal. */}
      <path
        className="game-trophy-shade"
        d="M12 2.8h5v5.6c0 3.2-2.2 5.7-5 5.7ZM12 17.2h4.6c.6 0 1 .4 1 1v2.9H12Z"
      />
      <path
        className="game-trophy-light"
        d="M8.6 4.2h2.2v4.6c0 1.6.5 2.8 1.2 3.6-1.9-.3-3.4-2-3.4-4.3Z"
      />
    </svg>
  );
}

/** A solid sword for attacks; takes the text colour. */
export function SwordIcon() {
  return (
    <svg className="game-mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M21.2 2.8v2.6l-9.6 9.6-2.6-2.6 9.6-9.6Z" />
      <path className="game-mark-light" d="M21.2 2.8 10.3 13.7l-1.3-1.3 9.6-9.6Z" />
      <path d="m6.9 12.3 4.8 4.8-1.4 1.4-1.3-1.3-3.3 3.3a1.6 1.6 0 1 1-2.2-2.2L6.8 15l-1.3-1.3Z" />
    </svg>
  );
}

/** A solid shield for defenses; takes the text colour. */
export function ShieldIcon() {
  return (
    <svg className="game-mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 2.4 20 5.4v6c0 5-3.4 8.5-8 10.2-4.6-1.7-8-5.2-8-10.2v-6Z" />
      <path className="game-mark-light" d="M12 2.4v19.2c-4.6-1.7-8-5.2-8-10.2v-6Z" />
    </svg>
  );
}

// Warm crest colours only, so every crest sits inside the ember palette.
const CREST_FILLS = ["#c2410c", "#b45309", "#9a5b13", "#a8322a", "#8c3a1a", "#c0561c"];

/** A shield crest with the first letter of `name`, coloured from the name so
 *  the same Clasher or crew always gets the same crest. */
export function Crest({ name }: { name: string }) {
  const letter = Array.from(name.trim())[0]?.toUpperCase() ?? "?";
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)!) >>> 0;
  const fill = CREST_FILLS[hash % CREST_FILLS.length];
  return (
    <svg className="crest" viewBox="0 0 40 44" aria-hidden="true" focusable="false">
      <path
        className="crest-body"
        style={{ fill }}
        d="M20 2 37 7v13c0 11-7.5 18.5-17 22C10.5 38.5 3 31 3 20V7Z"
      />
      <path className="crest-light" d="M20 2v40C10.5 38.5 3 31 3 20V7Z" />
      <path
        className="crest-rim"
        d="M20 5.6 34 9.8V20c0 9.2-6 15.6-14 18.8C12 35.6 6 29.2 6 20V9.8Z"
      />
      <text className="crest-letter" x="20" y="27.5" textAnchor="middle">
        {letter}
      </text>
    </svg>
  );
}
