export function formatNet(value: number | null): string {
  if (value === null) return "Unknown";
  return value > 0 ? `+${value}` : String(value);
}
