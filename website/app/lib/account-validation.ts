/**
 * Account name rules mirror the Python account domain (python/src/clashlens/accounts.py).
 * The Python private API remains authoritative; this module gives early feedback only.
 */

import { normalizePlayerTag } from "./player-tag";

const USERNAME_PATTERN = /^[a-z][a-z0-9_]{2,31}$/;

export const RESERVED_USERNAMES: ReadonlySet<string> = new Set([
  "account",
  "admin",
  "analytics",
  "api",
  "clashlens",
  "groups",
  "leaderboard",
  "login",
  "logout",
  "players",
  "settings",
  "support",
  "users",
]);

export const MAX_NAME_LENGTH = 80;
export const MAX_GROUP_TAGS = 100;
export const MAX_VERIFICATION_TOKEN_LENGTH = 512;

/** Canonical player tag for account forms, which accept an optional leading `#`. */
export function normalizeSubmittedPlayerTag(value: string): string | null {
  const trimmed = value.trim();
  return normalizePlayerTag(trimmed.startsWith("#") ? trimmed : `#${trimmed}`);
}

/** Normalized username or null when the value does not meet the Python rules. */
export function normalizeUsername(value: string): string | null {
  const normalized = value.trim().toLowerCase();
  if (!USERNAME_PATTERN.test(normalized)) return null;
  if (RESERVED_USERNAMES.has(normalized)) return null;
  return normalized;
}

/** NFC-normalized display name without surrounding space, or null when invalid. */
export function normalizeDisplayName(value: string): string | null {
  return normalizeName(value);
}

/** NFC-normalized group name without surrounding space, or null when invalid. */
export function normalizeGroupName(value: string): string | null {
  return normalizeName(value);
}

function normalizeName(value: string): string | null {
  const normalized = value.normalize("NFC").trim();
  if (normalized.length === 0 || normalized.length > MAX_NAME_LENGTH) return null;
  if (/[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}]/u.test(normalized)) return null;
  return normalized;
}

/**
 * Normalize a submitted value for the strict inappropriate-name check.
 * The filter is intentionally strict and may reject innocent names.
 */
function filterNormalize(value: string): string {
  const casefolded = value.toLowerCase();
  const decomposed = casefolded
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .replace(/@/g, "a")
    .replace(/r0/g, "or")
    .replace(/0/g, "o")
    .replace(/[1!|*]/g, "i")
    .replace(/3/g, "e")
    .replace(/4/g, "a")
    .replace(/[5$]/g, "s")
    .replace(/7/g, "t")
    .replace(/8/g, "b")
    .replace(/9/g, "g")
    .replace(/2/g, "z")
    .replace(/6/g, "g")
    .replace(/v/g, "u");
  return decomposed.replace(/[^a-z0-9]/g, "");
}

/**
 * Strict early-feedback blocklist after normalization.
 *
 * This list is NOT authoritative. The Python account domain owns the production
 * inappropriate-name rule. Python rejection always wins. The website never logs
 * or echoes a rejected value; it returns one generic safe field error.
 */
const INAPPROPRIATE_TERMS: readonly string[] = [
  "anal",
  "anus",
  "arse",
  "asshole",
  "bastard",
  "bitch",
  "blowjob",
  "bollocks",
  "boner",
  "bullshit",
  "butthole",
  "clitoris",
  "cock",
  "cocksucker",
  "cunt",
  "dick",
  "dildo",
  "douche",
  "fag",
  "faggot",
  "fuck",
  "gangbang",
  "gook",
  "handjob",
  "hitler",
  "homo",
  "jackass",
  "jerkoff",
  "kike",
  "killyourself",
  "kys",
  "nazi",
  "nigg",
  "nipple",
  "orgasm",
  "paki",
  "pedo",
  "penis",
  "piss",
  "porn",
  "prick",
  "pussy",
  "queef",
  "rape",
  "rapist",
  "retard",
  "scrotum",
  "sex",
  "shit",
  "slut",
  "spic",
  "suckmydick",
  "tits",
  "titty",
  "tranny",
  "twat",
  "vagina",
  "wank",
  "whore",
];

export function isInappropriateName(value: string): boolean {
  const normalized = filterNormalize(value);
  if (normalized.length === 0) return false;
  return INAPPROPRIATE_TERMS.some((term) => normalized.includes(term));
}

/**
 * Normalize a submitted player-tag list: canonical tags, unique, bounded.
 * Returns null when any tag is invalid or the list exceeds the group limit.
 */
export function normalizeTagList(
  values: string[],
  limit = MAX_GROUP_TAGS,
): string[] | null {
  if (values.length > limit) return null;
  const tags = new Set<string>();
  for (const value of values) {
    const normalized = normalizeSubmittedPlayerTag(value);
    if (normalized === null) return null;
    tags.add(normalized);
  }
  return [...tags].sort();
}
