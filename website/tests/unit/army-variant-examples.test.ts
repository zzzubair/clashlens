import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
  process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 =
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
});

import { ExampleArmies } from "../../app/components/VariantExamples";
import { loader as examplesLoader } from "../../app/routes/army-variant-examples";

// Revenge Thrower: 9 Throwers and 4 Healers, a Duke with Revenge Deck, and
// the same army with a Wall Wrecker.
const REVENGE = "h7p4e60_52u9x132-4x7s2x109";
const REVENGE_SIEGE = "h7p4e60_52u9x132-4x7-1x51s2x109";

function pythonReplies(payload: unknown, status = 200) {
  const fetchMock = vi.fn().mockImplementation(
    async () =>
      new Response(JSON.stringify(payload), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const examplesPayload = {
  kind: "army-variant-examples",
  type_id: 3,
  variant_id: 3,
  rule: "With Revenge Deck, without Fireball or Rocket Backpack.",
  selection: { season: "1776052800", start_day: 1, end_day: 6 },
  covered_days: [1, 3, 4, 5, 6],
  groups: [
    { attacks: 5, armies: 3, example_attacks: 3, army_share_code: REVENGE_SIEGE },
    { attacks: 1, armies: 1, example_attacks: 1, army_share_code: REVENGE },
  ],
  army_units: {
    "troop:132": { name: "Thrower", siege: false },
    "troop:51": { name: "Wall Wrecker", siege: true },
  },
};

const PAGE_DAYS = "population=top-1000&season=1776052800&days=1,3,4,5,6&type=3&variant=3";

function examplesRequest(query: string) {
  return {
    request: new Request(
      `https://clashlens.example/resources/analytics/armies/examples?${query}`,
    ),
    params: {},
  } as never;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("variant example armies", () => {
  it("asks for the variant's armies for the page's players this Season", async () => {
    const fetchMock = pythonReplies(examplesPayload);
    const reply = await examplesLoader(examplesRequest(PAGE_DAYS));
    const target = new URL(fetchMock.mock.calls[0][0] as URL);
    expect(target.pathname).toBe("/v1/analytics/armies");
    expect(Object.fromEntries(target.searchParams)).toEqual({
      season: "current",
      start_day: "1",
      end_day: "6",
      population: "top-1000",
      category: "army-types",
      type_id: "3",
      variant_id: "3",
    });
    expect(reply.data.examples).toEqual({
      rule: examplesPayload.rule,
      groups: [
        { attacks: 5, armyShareCode: REVENGE_SIEGE },
        { attacks: 1, armyShareCode: REVENGE },
      ],
      armyUnits: examplesPayload.army_units,
    });
  });

  it("says the armies could not load instead of failing the page", async () => {
    pythonReplies({ ...examplesPayload, groups: [{ attacks: 5 }] });
    const malformed = await examplesLoader(examplesRequest(PAGE_DAYS));
    expect(malformed.data.examples).toBeNull();
    // Days published since the page loaded: other days than the page's totals.
    pythonReplies({
      ...examplesPayload,
      selection: { ...examplesPayload.selection, end_day: 7 },
      covered_days: [1, 3, 4, 5, 6, 7],
    });
    const later = await examplesLoader(examplesRequest(PAGE_DAYS));
    expect(later.data.examples).toBeNull();
    // A missing day published since: the same first and last day, more days.
    pythonReplies({ ...examplesPayload, covered_days: [1, 2, 3, 4, 5, 6] });
    const filled = await examplesLoader(examplesRequest(PAGE_DAYS));
    expect(filled.data.examples).toBeNull();
    pythonReplies({ error: "invalid_army_analytics_selection" }, 422);
    const refused = await examplesLoader(
      examplesRequest(PAGE_DAYS.replace("type=3", "type=99")),
    );
    expect(refused.data.examples).toBeNull();
    const fetchMock = pythonReplies(examplesPayload);
    const invalid = await examplesLoader(
      examplesRequest(PAGE_DAYS.replace("top-1000", "all")),
    );
    expect(invalid.init?.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("variant example armies panel", () => {
  it("shows each group's attacks, share of the variant, army and Copy army", () => {
    const html = renderToString(
      createElement(ExampleArmies, {
        id: "army-variant:3:3",
        label: "Revenge Thrower",
        attacks: 6,
        examples: {
          rule: examplesPayload.rule,
          groups: [
            { attacks: 5, armyShareCode: REVENGE_SIEGE },
            { attacks: 1, armyShareCode: REVENGE },
          ],
          armyUnits: examplesPayload.army_units,
        },
      }),
    );
    const text = html
      .replaceAll("<!-- -->", "")
      .replace(/<[^>]*>/g, " ")
      .replace(/\s+/g, " ");
    expect(text).toContain(examplesPayload.rule);
    // Shares of the variant's 6 attacks, most used first.
    expect(text).toContain("83.3% 5 attacks");
    expect(text).toContain("16.7% 1 attack");
    expect(text).toContain("Near-identical armies count as one.");
    // The first group shows; the other waits for Show more.
    expect(html.match(/<li hidden=""/g)).toHaveLength(1);
    expect(text).toContain("Show 1 more army");
    expect(html).toContain('title="9 × Thrower"');
    expect(html).toContain("Wall Wrecker");
    expect(
      [...html.matchAll(/href="([^"]*CopyArmy[^"]*)"/g)].map(([, href]) => href),
    ).toEqual(
      [REVENGE_SIEGE, REVENGE].map(
        (code) =>
          `https://link.clashofclans.com/en?action=CopyArmy&amp;army=${encodeURIComponent(code)}`,
      ),
    );
    expect(html).toContain(
      'aria-label="Copy Revenge Thrower army 1, opens in a new tab"',
    );
  });
});
