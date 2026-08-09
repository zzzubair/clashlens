/**
 * Response contracts for the private Python account API.
 *
 * The field shapes and error codes below mirror the exact payloads produced by
 * python/src/clashlens/api.py and python/src/clashlens/api_db.py. Do not invent
 * fields; a payload that does not validate is rejected as malformed.
 */

import { normalizePlayerTag } from "./player-tag";

export interface ClashLensAccount {
  username: string;
  displayName: string;
  preferences: Record<string, unknown>;
  providers: string[];
}

export interface VerifiedPlayer {
  tag: string;
  name: string | null;
}

export interface AccountSummary {
  username: string;
  displayName: string;
  verifiedPlayers: VerifiedPlayer[];
}

export interface SavedPlayer {
  tag: string;
  name: string | null;
}

export interface SavedTagResult {
  tag: string;
  saved: boolean;
}

export interface PrivateGroup {
  groupId: string;
  name: string;
  tags: string[];
}

export interface GroupDeleteResult {
  groupId: string;
  deleted: boolean;
}

export interface PublicUser {
  username: string;
  displayName: string;
  verifiedPlayers: VerifiedPlayer[];
}

export type VerificationStatus =
  | "linked"
  | "already_linked"
  | "invalid_token"
  | "verification_unavailable"
  | "support_required"
  | "in_progress";

export interface VerificationResult {
  status: VerificationStatus;
  /**
   * Canonical player tag. The Python API omits it for the in_progress replay
   * outcome (202), so it is optional only for that status.
   */
  tag?: string;
  verificationRequestId?: string;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNullableString(value: unknown): value is string | null {
  return value === null || isString(value);
}

function isCanonicalTag(value: unknown): value is string {
  return isString(value) && normalizePlayerTag(value) === value;
}

interface AccountPayload {
  username: string;
  display_name: string;
  preferences: Record<string, unknown>;
  providers: string[];
}

function asAccountPayload(value: unknown): AccountPayload | null {
  if (!isRecord(value)) return null;
  const { username, display_name, preferences, providers } = value;
  if (
    !isString(username) ||
    username.length === 0 ||
    !isString(display_name) ||
    display_name.length === 0 ||
    !isRecord(preferences) ||
    !Array.isArray(providers) ||
    !providers.every(isString)
  ) {
    return null;
  }
  return { username, display_name, preferences, providers: [...providers] };
}

export function mapAccount(value: unknown): ClashLensAccount | null {
  const payload = asAccountPayload(value);
  if (payload === null) return null;
  return {
    username: payload.username,
    displayName: payload.display_name,
    preferences: payload.preferences,
    providers: payload.providers,
  };
}

function asVerifiedPlayer(value: unknown): VerifiedPlayer | null {
  if (!isRecord(value) || !isCanonicalTag(value.tag) || !isNullableString(value.name)) {
    return null;
  }
  return { tag: value.tag, name: value.name };
}

interface NamePayload {
  username: string;
  display_name: string;
  verified_players: VerifiedPlayer[];
}

function asNamePayload(value: unknown): NamePayload | null {
  if (!isRecord(value)) return null;
  const { username, display_name, verified_players } = value;
  if (
    !isString(username) ||
    username.length === 0 ||
    !isString(display_name) ||
    display_name.length === 0 ||
    !Array.isArray(verified_players)
  ) {
    return null;
  }
  const players: VerifiedPlayer[] = [];
  for (const entry of verified_players) {
    const player = asVerifiedPlayer(entry);
    if (player === null) return null;
    players.push(player);
  }
  return { username, display_name, verified_players: players };
}

export function mapSummary(value: unknown): AccountSummary | null {
  const payload = asNamePayload(value);
  if (payload === null) return null;
  return {
    username: payload.username,
    displayName: payload.display_name,
    verifiedPlayers: payload.verified_players,
  };
}

export function mapSavedTags(value: unknown): SavedPlayer[] | null {
  if (!isRecord(value) || !Array.isArray(value.players)) return null;
  const players: SavedPlayer[] = [];
  for (const entry of value.players) {
    const player = asVerifiedPlayer(entry);
    if (player === null) return null;
    players.push(player);
  }
  return players;
}

export function mapSavedTagResult(value: unknown): SavedTagResult | null {
  if (
    !isRecord(value) ||
    !isCanonicalTag(value.tag) ||
    typeof value.saved !== "boolean"
  ) {
    return null;
  }
  return { tag: value.tag, saved: value.saved };
}

interface GroupPayload {
  group_id: string;
  name: string;
  tags: string[];
}

function asGroupPayload(value: unknown): GroupPayload | null {
  if (!isRecord(value)) return null;
  const { group_id, name, tags } = value;
  if (
    !isString(group_id) ||
    !UUID_PATTERN.test(group_id) ||
    !isString(name) ||
    name.length === 0 ||
    !Array.isArray(tags) ||
    !tags.every(isCanonicalTag)
  ) {
    return null;
  }
  return { group_id, name, tags: [...tags] };
}

export function mapGroups(value: unknown): PrivateGroup[] | null {
  if (!isRecord(value) || !Array.isArray(value.groups)) return null;
  const groups: PrivateGroup[] = [];
  for (const entry of value.groups) {
    const payload = asGroupPayload(entry);
    if (payload === null) return null;
    groups.push({ groupId: payload.group_id, name: payload.name, tags: payload.tags });
  }
  return groups;
}

export function mapGroupResult(value: unknown): PrivateGroup | null {
  const payload = asGroupPayload(value);
  if (payload === null) return null;
  return { groupId: payload.group_id, name: payload.name, tags: payload.tags };
}

export function mapGroupDeleteResult(value: unknown): GroupDeleteResult | null {
  if (
    !isRecord(value) ||
    typeof value.deleted !== "boolean" ||
    !isString(value.group_id) ||
    !UUID_PATTERN.test(value.group_id)
  ) {
    return null;
  }
  return { groupId: value.group_id, deleted: value.deleted };
}

export function mapPublicUser(value: unknown): PublicUser | null {
  const payload = asNamePayload(value);
  if (payload === null) return null;
  return {
    username: payload.username,
    displayName: payload.display_name,
    verifiedPlayers: payload.verified_players,
  };
}

const VERIFICATION_STATUSES: readonly VerificationStatus[] = [
  "linked",
  "already_linked",
  "invalid_token",
  "verification_unavailable",
  "support_required",
  "in_progress",
];

export function mapVerificationResult(value: unknown): VerificationResult | null {
  if (!isRecord(value)) return null;
  const { status, tag } = value;
  if (
    !isString(status) ||
    !VERIFICATION_STATUSES.includes(status as VerificationStatus)
  ) {
    return null;
  }
  if (status !== "in_progress" && !isCanonicalTag(tag)) return null;
  if (status === "in_progress" && tag !== undefined && !isCanonicalTag(tag)) return null;
  const result: VerificationResult = { status: status as VerificationStatus };
  if (isCanonicalTag(tag)) result.tag = tag;
  const requestId = value.verification_request_id;
  if (isString(requestId) && UUID_PATTERN.test(requestId)) {
    result.verificationRequestId = requestId;
  }
  return result;
}
