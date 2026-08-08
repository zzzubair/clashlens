const PLAYER_TAG_PATTERN = /^#[0289PYLQGRJCUV]{3,15}$/;
export const MAX_PLAYER_TAG_INPUT_LENGTH = 64;

export function normalizePlayerTag(value: string): string | null {
  if (value.length > MAX_PLAYER_TAG_INPUT_LENGTH) return null;
  const normalized = value.trim().toUpperCase();
  return PLAYER_TAG_PATTERN.test(normalized) ? normalized : null;
}

export function validPlayerTag(value: string): boolean {
  return normalizePlayerTag(value) === value;
}

export function canonicalPlayerPath(value: string): string {
  const normalized = normalizePlayerTag(value);
  if (!normalized) {
    throw new Error("Invalid Clash player tag");
  }
  return `/players/${encodeURIComponent(normalized)}`;
}
