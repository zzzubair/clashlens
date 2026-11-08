/** Signed counts keep each view's existing zero, precision and locale. */
export function signedNumber(
  value: number,
  {
    locale = "en-US",
    digits,
    zero = "0",
  }: { locale?: string; digits?: number; zero?: string } = {},
): string {
  const text = Math.abs(value).toLocaleString(
    locale,
    digits === undefined
      ? undefined
      : { minimumFractionDigits: digits, maximumFractionDigits: digits },
  );
  return value > 0
    ? `+${text}`
    : value < 0
      ? `−${text}`
      : value === 0
        ? digits === undefined
          ? zero
          : text
        : text;
}

/** A signed number rounded to `digits` places; "–" when unknown. */
export function signedOr(value: number | null, digits = 0): string {
  if (value === null || !Number.isFinite(value)) return "–";
  const rounded = Number(value.toFixed(digits));
  return signedNumber(rounded, { digits });
}

export function formatCount(value: number | null): string {
  return value === null ? "Unknown" : (value || 0).toLocaleString("en-GB");
}

export function formatSigned(value: number | null): string {
  if (value === null) return "Unknown";
  return signedNumber(value || 0, { locale: "en-GB" });
}

export function valueTone(value: number | null): string {
  if (value === null || value === 0) return "score-neutral";
  return value > 0 ? "score-positive" : "score-negative";
}

export const signedSide = (value: number | null, sign: 1 | -1, digits = 0) =>
  value === null
    ? "Unavailable"
    : value === 0
      ? "0"
      : `${sign > 0 ? "+" : "−"}${value.toLocaleString("en-GB", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        })}`;

export function signedAverage(value: number, negative: boolean, digits: number): string {
  const text = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return value === 0 ? text : `${negative ? "−" : "+"}${text}`;
}
