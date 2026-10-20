const PLAYER_TAG_PATTERN = /^#[0289PYLQGRJCUV]{3,15}$/;
export const MAX_PLAYER_TAG_INPUT_LENGTH = 64;

export function normalizePlayerTag(value: string): string | null {
  if (value.length > MAX_PLAYER_TAG_INPUT_LENGTH) return null;
  const normalized = value.trim().toUpperCase();
  return PLAYER_TAG_PATTERN.test(normalized) ? normalized : null;
}

/**
 * What to say when a search finds nobody: a complete-looking #tag with
 * letters the game never uses is not a tag at all, so asking for a full #tag
 * would be wrong.
 */
export function noMatchHint(query: string): string {
  const typed = query.trim().toUpperCase();
  return /^#[0-9A-Z]{3,15}$/.test(typed) && normalizePlayerTag(typed) === null
    ? `${typed} is not a Clash of Clans player tag. Tags use only 0, 2, 8, 9 and the letters P, Y, L, Q, G, R, J, C, U and V.`
    : "Enter a full #tag to look up anyone.";
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

/** Where a signed-in Clasher saves the player into one of their groups. */
export function addToGroupPath(tag: string): string {
  return `/account/groups/add/${tag.replace(/^#/, "")}`;
}
