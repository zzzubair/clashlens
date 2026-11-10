import { readAt } from "../../app/components/DashboardGrid";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it, expect } from "vitest";
import { updatedAgo } from "../../app/lib/age-format";
import { LegendClock, clockMarks } from "../../app/components/LegendClock";
import { LegendDayCard } from "../../app/components/LegendDayCard";
import type { PlayerDay } from "../../app/lib/dashboard";
import { emptyDay, fight, linked } from "../fixtures/dashboard-data";
const MAIN = "#LQ2V8PJ0";
const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
describe("Legend clock", () => {
  const dayStart = Date.UTC(2026, 9, 7, 5);

  it("puts each battle on its track at its own time, grouping those too close for two dots", () => {
    const marks = clockMarks(
      [
        fight("attack", 7, 10, 3, 40),
        fight("attack", 7, 25, 3, 40),
        fight("attack", 7, 44, 2, 31),
        fight("attack", 12, 6, 2, 29),
        fight("attack", 12, 50, 3, 40),
        fight("attack", 14, 0, 3, 40),
        fight("defense", 7, 30, 3, -40),
        fight("defense", 8, 20, 2, -24),
        fight("defense", 11, 0, 3, -40),
        fight("defense", 4, 0, 1, -16),
      ],
      dayStart,
    );
    // Minutes after the Reset, and how many battles each mark holds: the
    // three attacks round 07:25 share one mark at their middle time, as do
    // the 12:06 and 12:50 attacks and the 07:30 and 08:20 defenses; 14:00
    // and 11:00 keep their own time; the 04:00 defense is before this
    // Legend day began.
    const at = (kind: string) =>
      marks
        .filter((mark) => mark.kind === kind)
        .map((mark) => `${Math.round(mark.minutes)}×${mark.battles.length}`);
    expect(at("attack")).toEqual(["146×3", "448×2", "540×1"]);
    expect(at("defense")).toEqual(["175×2", "360×1"]);
  });

  it("names each mark's battles on it, and the Reset's time under the countdown", () => {
    const battles = [fight("attack", 7, 10, 3, 40), fight("attack", 7, 25, 2, 28)];
    const html = renderToStaticMarkup(
      createElement(LegendClock, {
        dayStartMs: Date.UTC(2026, 9, 7, 5),
        dayEndMs: Date.UTC(2026, 9, 8, 5),
        battles: [...battles, fight("defense", 11, 0, 1, -16)],
        nowMs: Date.UTC(2026, 9, 7, 9),
        timeZone: "UTC",
      }),
    );
    // Each mark names its battles: when, who, stars, % and trophies; two
    // attacks too close for two marks share one mark, drawn as two ticks.
    expect(html).toContain('title="Defense at 11:00 against Opponent: 1 star, 80%, −16');
    expect(html).toContain(":\n07:10 on Opponent: 3 stars, 100%, +40 trophies\n07:25");
    expect(html.match(/class="clock-tick clock-tick-attack"/g)).toHaveLength(2);
    expect(text(html)).toContain("05:00 11:00 17:00 23:00 Now 20h 00m to Reset at 05:00");
  });

  it("names the upcoming Reset in local time across a clock change", () => {
    const html = renderToStaticMarkup(
      createElement(LegendClock, {
        dayStartMs: Date.UTC(2026, 9, 24, 5),
        dayEndMs: Date.UTC(2026, 9, 25, 5),
        battles: [],
        nowMs: Date.UTC(2026, 9, 25, 2),
        timeZone: "Europe/London",
      }),
    );
    // The day began at 06:00 summer time; its Reset falls at 05:00 winter time.
    expect(html).toContain('aria-label="Legend clock: 3h 00m to Reset at 05:00"');
    expect(text(html)).toContain("3h 00m to Reset at 05:00");
  });
});

describe("Legend day card", () => {
  it("says once, in its header, how old the older of its two reads is", () => {
    const at = (minutes: number) => Date.UTC(2026, 9, 7, 9, 0) - minutes * 60_000;
    const range = { best: 95, worst: 760 };
    const data = (day: PlayerDay | null) =>
      ({
        player: linked(MAIN),
        day,
        range,
        rangeAtMs: at(9),
        nowMs: at(0),
      }) as const;
    const updated = (day: PlayerDay | null) =>
      updatedAgo(readAt("legend-day", { ...data(day), projected: null }), at(0));
    expect(updated(null)).toBe("updated 9 min ago");
    expect(updated(emptyDay({ observedAtMs: at(2) }))).toBe("updated 9 min ago");
    expect(updated(emptyDay({ observedAtMs: at(12) }))).toBe("updated 12 min ago");
    // 09:00 UTC is 20 hours before the 05:00 Reset; the title's name is for screen readers.
    const card = text(
      renderToStaticMarkup(
        createElement(LegendDayCard, data(emptyDay({ dayNumber: 9 }))),
      ),
    );
    expect(card).toContain("Legend day 9 , Lens Main Updated 9 min ago Trophies");
    expect(card).toContain("20h 00m to Reset");
  });

  const render = (
    day: PlayerDay | null,
    range = { best: 95, worst: 760 },
    projected: number | null = null,
  ) =>
    text(
      renderToStaticMarkup(
        createElement(LegendDayCard, {
          player: linked(MAIN),
          day,
          range,
          projected,
        }),
      ),
    );

  it("shows the projected place beside the outer range, labelled its range", () => {
    const html = render(emptyDay({}), { best: 1, worst: 12_270 }, 3000);
    expect(html).toContain("Projected place #3,000 at Reset. ");
    expect(html).toContain(
      "left, and gets closer through the day. Next Reset range best #1 worst #12,270",
    );
    expect(html).toContain(
      "An estimate from how players in your spot finished on recent days. It can be hundreds of places out while battles are left, and gets closer through the day.",
    );
    // Before any day's examples are saved, only the outer range shows.
    expect(render(emptyDay({}))).not.toContain("Projected");
  });

  it("shows trophies, net, the ranks and the next Reset range as an estimate", () => {
    const html = render(
      emptyDay({
        trophies: 5702,
        complete: true,
        net: 66,
        gained: 71,
        lost: -5,
        attacks: 2,
        defenses: 1,
        lastResetRank: 331,
        openDefenses: 7,
        autoDefenseEach: 21,
        battles: [
          fight("attack", 7, 0, 3, 40),
          fight("attack", 8, 0, 2, 31),
          fight("defense", 9, 0, 1, -5),
        ],
      }),
    );
    expect(html).toContain("Trophies 5,702");
    // The change is marked "today" on screen, not left as a bare number.
    expect(html).toContain("Net today +66 today");
    expect(html).toContain("Last Reset #331");
    expect(html).toContain("Now #318");
    expect(html).toContain("Next Reset best #95 – worst #760 estimate");
    expect(html).toContain("Attacks 2/8 +71");
    expect(html).toContain("Defenses 1/8 −5");
    // The defenses still to come and their automatic loss stay off the card.
    expect(html).not.toContain("defenses remain");
    expect(html).not.toContain("auto defense");
  });

  it("leaves totals and the automatic loss out until they are known", () => {
    const html = render(
      emptyDay({ attacks: 3, defenses: 2, openDefenses: 6, battles: [] }),
    );
    expect(html).toContain("Attacks 3/8 Defenses 2/8 ");
    expect(html).not.toContain("3/8 –");
    // A screen reader hears each row's count with its noun.
    const markup = renderToStaticMarkup(
      createElement(LegendDayCard, {
        player: linked(MAIN),
        day: emptyDay({ attacks: 3, defenses: 2, battles: [] }),
        range: null,
        projected: null,
      }),
    );
    expect(markup).toContain('aria-label="3 of 8 attacks"');
    expect(markup).toContain('aria-label="2 of 8 defenses"');
    expect(render(null)).toContain("Trophies 5,696 Net today –");
  });

  it("shows – for attack and defense counts it does not know, not 0", () => {
    // Today's battles not saved yet, or the player page not read at all.
    for (const day of [emptyDay({ lastResetRank: 331 }), null]) {
      const html = render(day);
      expect(html).toContain("Attacks –/8");
      expect(html).toContain("Defenses –/8");
      expect(html).not.toContain("0/8");
      expect(html).toContain("Trophies 5,696");
      expect(html).toContain("Now #318");
      expect(html).toContain("Next Reset best #95 – worst #760");
    }
  });
});
