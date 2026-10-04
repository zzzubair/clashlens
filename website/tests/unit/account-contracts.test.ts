import { describe, expect, it } from "vitest";

import {
  mapAccount,
  mapGroupDeleteResult,
  mapGroupResult,
  mapGroupPlayer,
  mapGroups,
  mapPublicUser,
  mapSavedTagResult,
  mapSavedTags,
  mapSummary,
  mapVerificationResult,
} from "../../app/lib/account-contracts";

describe("Python account response mappers", () => {
  it("maps the account payload", () => {
    const result = mapAccount({
      username: "nova",
      display_name: "Nova",
      preferences: {},
      providers: ["google"],
    });
    expect(result).toEqual({
      username: "nova",
      displayName: "Nova",
      preferences: {},
      providers: ["google"],
    });
  });

  it("rejects a malformed account payload", () => {
    expect(mapAccount({ username: "nova", display_name: "Nova" })).toBeNull();
    expect(
      mapAccount({ username: "", display_name: "Nova", preferences: {}, providers: [] }),
    ).toBeNull();
    expect(
      mapAccount({
        username: "nova",
        display_name: "Nova",
        preferences: "x",
        providers: [],
      }),
    ).toBeNull();
    expect(
      mapAccount({
        username: "nova",
        display_name: "Nova",
        preferences: [],
        providers: [],
      }),
    ).toBeNull();
    expect(
      mapAccount({
        username: "nova",
        display_name: "Nova",
        preferences: {},
        providers: [7],
      }),
    ).toBeNull();
    expect(mapAccount(null)).toBeNull();
  });

  it("maps the summary and public user payloads", () => {
    const payload = {
      username: "nova",
      display_name: "Nova",
      verified_players: [
        { tag: "#2PP", name: "Nova" },
        { tag: "#2PQ", name: null },
      ],
    };
    expect(mapSummary(payload)).toEqual({
      username: "nova",
      displayName: "Nova",
      verifiedPlayers: [
        { tag: "#2PP", name: "Nova" },
        { tag: "#2PQ", name: null },
      ],
    });
  });

  it("maps each public profile card and rejects a malformed one", () => {
    const card = {
      tag: "#2PP",
      name: "Nova",
      clan: "Night Owls",
      state: "tracking",
      reason: null,
      trophies: 5300,
      season_reset_pending: false,
      rank: 12,
      today: { net: -8, attacks: 5, defenses: 3 },
    };
    const season0 = {
      ...card,
      tag: "#2PQ",
      name: null,
      clan: null,
      reason: "no_legend_battles",
      trophies: null,
      rank: null,
      today: null,
    };
    const user = { username: "nova", display_name: "Nova" };
    expect(mapPublicUser({ ...user, verified_players: [card, season0] })).toEqual({
      username: "nova",
      displayName: "Nova",
      verifiedPlayers: [
        {
          tag: "#2PP",
          name: "Nova",
          clan: "Night Owls",
          state: "tracking",
          reason: null,
          trophies: 5300,
          seasonResetPending: false,
          rank: 12,
          today: { net: -8, attacks: 5, defenses: 3 },
        },
        {
          tag: "#2PQ",
          name: null,
          clan: null,
          state: "tracking",
          reason: "no_legend_battles",
          trophies: null,
          seasonResetPending: false,
          rank: null,
          today: null,
        },
      ],
    });
    for (const broken of [
      { ...card, state: "ranked" },
      { ...card, reason: "other" },
      { ...card, rank: 0 },
      { ...card, trophies: "5300" },
      { ...card, today: { net: 1, attacks: -1, defenses: 0 } },
      { tag: "#2PP", name: "Nova" },
    ]) {
      expect(mapPublicUser({ ...user, verified_players: [broken] })).toBeNull();
    }
  });

  it("rejects malformed summary and public user payloads", () => {
    expect(
      mapSummary({ username: "nova", display_name: "Nova", verified_players: [{}] }),
    ).toBeNull();
    expect(
      mapSummary({
        username: "nova",
        display_name: "Nova",
        verified_players: [{ tag: "#bad", name: null }],
      }),
    ).toBeNull();
    expect(
      mapPublicUser({ username: "nova", display_name: "Nova", verified_players: "x" }),
    ).toBeNull();
    expect(mapSummary({})).toBeNull();
  });

  it("maps saved tag lists and results", () => {
    expect(mapSavedTags({ players: [{ tag: "#2PP", name: "Nova" }] })).toEqual([
      { tag: "#2PP", name: "Nova" },
    ]);
    expect(mapSavedTags({ players: [{ tag: "#2PP", name: 4 }] })).toBeNull();
    expect(mapSavedTags({ players: "x" })).toBeNull();
    expect(mapSavedTagResult({ tag: "#2PP", saved: true })).toEqual({
      tag: "#2PP",
      saved: true,
    });
    expect(mapSavedTagResult({ tag: "#2PP", saved: "yes" })).toBeNull();
    expect(mapSavedTagResult({ tag: "bad", saved: true })).toBeNull();
  });

  it("maps group lists, results, and deletion results", () => {
    const group = {
      group_id: "0e4e5f1a-8d4c-4b6e-9f1a-2b3c4d5e6f70",
      name: "Push team",
      tags: ["#2PP", "#2PL"],
    };
    const players = [
      { tag: "#2PP", name: "Nova", trophies: 5400, state: "tracking" },
      { tag: "#2PL", name: null, trophies: null, state: "not_in_legend" },
    ];
    const season = "1791176400";
    expect(mapGroups({ season, groups: [{ ...group, players }] })).toEqual({
      season,
      groups: [
        {
          groupId: group.group_id,
          name: "Push team",
          tags: ["#2PP", "#2PL"],
          players: players.map((player) => ({ ...player, seasonResetPending: false })),
        },
      ],
    });
    // Without the Season, an open page could not tell when its trophies expire.
    expect(mapGroups({ groups: [{ ...group, players }] })).toBeNull();
    expect(mapGroups({ season, groups: [group] })).toBeNull();
    expect(
      mapGroups({
        season,
        groups: [{ ...group, players: [{ ...players[0], state: "gone" }] }],
      }),
    ).toBeNull();
    expect(mapGroupResult(group)).toEqual({
      groupId: group.group_id,
      name: "Push team",
      tags: ["#2PP", "#2PL"],
    });
    expect(mapGroupDeleteResult({ deleted: true, group_id: group.group_id })).toEqual({
      deleted: true,
      groupId: group.group_id,
    });
    expect(
      mapGroups({ season, groups: [{ ...group, players, group_id: "not-a-uuid" }] }),
    ).toBeNull();
    expect(
      mapGroups({ season, groups: [{ ...group, players, tags: ["#bad"] }] }),
    ).toBeNull();
    expect(mapGroups({ season, groups: "x" })).toBeNull();
    expect(mapGroupResult({ ...group, name: "" })).toBeNull();
    expect(mapGroupDeleteResult({ deleted: true, group_id: "x" })).toBeNull();
    expect(mapGroupDeleteResult({ deleted: "yes", group_id: group.group_id })).toBeNull();
  });

  it("preserves the Season wait in member and add responses", () => {
    const waiting = {
      tag: "#2PP",
      name: "Nova",
      trophies: null,
      state: "tracking",
      season_reset_pending: true,
    };
    expect(mapGroupPlayer(waiting)).toMatchObject({
      trophies: null,
      seasonResetPending: true,
    });
    expect(mapGroupPlayer({ ...waiting, season_reset_pending: "true" })).toBeNull();
  });

  it("maps every verification outcome payload", () => {
    const requestId = "7c3a2f1e-9d4b-4a6e-b2c3-d4e5f6a7b8c9";
    expect(mapVerificationResult({ status: "linked", tag: "#2PP" })).toEqual({
      status: "linked",
      tag: "#2PP",
    });
    expect(mapVerificationResult({ status: "already_linked", tag: "#2PP" })).toEqual({
      status: "already_linked",
      tag: "#2PP",
    });
    expect(mapVerificationResult({ status: "invalid_token", tag: "#2P8" })).toEqual({
      status: "invalid_token",
      tag: "#2P8",
    });
    expect(
      mapVerificationResult({ status: "verification_unavailable", tag: "#2PY" }),
    ).toEqual({
      status: "verification_unavailable",
      tag: "#2PY",
    });
    expect(
      mapVerificationResult({
        status: "support_required",
        tag: "#2PL",
        verification_request_id: requestId,
      }),
    ).toEqual({
      status: "support_required",
      tag: "#2PL",
      verificationRequestId: requestId,
    });
    expect(mapVerificationResult({ status: "in_progress", tag: "#2PP" })).toEqual({
      status: "in_progress",
      tag: "#2PP",
    });
    expect(mapVerificationResult({ status: "in_progress" })).toEqual({
      status: "in_progress",
    });
  });

  it("rejects malformed verification payloads", () => {
    expect(mapVerificationResult({ status: "linked" })).toBeNull();
    expect(mapVerificationResult({ status: "mystery", tag: "#2PP" })).toBeNull();
    expect(mapVerificationResult({ status: "linked", tag: "#bad" })).toBeNull();
    expect(
      mapVerificationResult({
        status: "support_required",
        tag: "#2PL",
        verification_request_id: "not-a-uuid",
      }),
    ).toEqual({ status: "support_required", tag: "#2PL" });
    expect(mapVerificationResult(null)).toBeNull();
  });
});
