/** The small circled "i" that opens an explanation. Drawn, because the
 * site's fonts have no ⓘ and some phones show a box in its place. */
export function InfoMark() {
  return (
    <svg className="info-mark" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <circle cx="8" cy="8" r="6.75" />
      <path d="M8 7.25v4M8 4.75v.01" />
    </svg>
  );
}
