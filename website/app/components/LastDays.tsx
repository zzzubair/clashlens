import "../last-days.css";

/**
 * The heading over a table's daily bars, as Rankings words its own: "Last 7
 * days", with the dates on hover and behind its ⓘ, and how the bars are
 * scaled said there once instead of under every heading.
 */
export function LastDays({ count, range }: { count: number; range: string | null }) {
  return (
    <span className="last-days">
      Last {count} days
      {range === null ? null : (
        <details className="last-days-info">
          <summary title={range}>
            <span aria-hidden="true">ⓘ</span>
            <span className="sr-only">Which days</span>
          </summary>
          <span className="last-days-body">
            {`${range}, one bar a day. Each player's bars have their own scale: the tallest is that player's biggest day.`}
          </span>
        </details>
      )}
    </span>
  );
}
