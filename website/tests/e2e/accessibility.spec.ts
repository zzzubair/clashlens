import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  ensureAccount,
  expectNoSeriousAccessibilityViolations,
  signIn,
} from "./helpers/account";

for (const [name, path, heading] of [
  ["home", "/", "Find any Legend League player"],
  ["player", "/players/%232PP", "Synthetic Clasher 001"],
  ["leaderboard", "/leaderboards/tracked", "Live Leaderboard"],
  ["login", "/login", "Sign in"],
] as const) {
  test(`${name} has no serious or critical accessibility violations`, async ({
    page,
  }) => {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
    await expectNoSeriousAccessibilityViolations(page);
  });
}

test("the skip link moves focus to the main content", async ({ page }) => {
  await page.goto("/");
  const skipLink = page.getByRole("link", { name: "Skip to main content" });
  await skipLink.focus();
  await page.keyboard.press("Enter");

  await expect(page.locator("#main-content")).toBeFocused();
});

// WCAG contrast between two [red, green, blue] colours.
function contrast(first: number[], second: number[]) {
  const luminance = (colour: number[]) => {
    const [red, green, blue] = colour.map((value) => {
      const channel = value / 255;
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
}

async function useTheme(page: Page, theme: "light" | "dark") {
  // Save the choice like the theme toggle does; the page resets an unsaved theme.
  await page.evaluate((value) => localStorage.setItem("clashlens-theme", value), theme);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

// Paints a text box's border onto its own fill and its ring onto what sits behind it.
// Behind is every fill under the box down to the first solid one, so a see-through
// card counts as the colour it shows over the page.
async function boxColours(box: Locator) {
  return box.evaluate((element) => {
    const style = getComputedStyle(element);
    const layers: string[] = [];
    for (let node = element.parentElement; node; node = node.parentElement) {
      const colour = getComputedStyle(node).backgroundColor;
      if (colour !== "rgba(0, 0, 0, 0)") layers.unshift(colour);
      if (/^rgb\(|^color\([^/]*\)$/.test(colour)) break;
    }
    const paint = (under: string, colour: string) => {
      const context = document.createElement("canvas").getContext("2d")!;
      for (const each of [under, colour]) {
        context.fillStyle = each;
        context.fillRect(0, 0, 1, 1);
      }
      return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
    };
    // The solid fill, then each see-through one over it.
    let behind = layers[0]!;
    for (const colour of layers.slice(1))
      behind = `rgb(${paint(behind, colour).join(" ")})`;
    const fill = paint(behind, style.backgroundColor);
    return {
      ringStyle: style.outlineStyle,
      ringWidth: parseFloat(style.outlineWidth),
      ring: paint(behind, style.outlineColor),
      behind: paint(behind, behind),
      border: paint(`rgb(${fill.join(" ")})`, style.borderTopColor),
      fill,
    };
  });
}

async function expectVisibleBorder(box: Locator) {
  const colours = await boxColours(box);
  expect(contrast(colours.border, colours.fill)).toBeGreaterThanOrEqual(3);
}

async function expectVisibleRing(box: Locator) {
  const colours = await boxColours(box);
  expect(colours.ringStyle).toBe("solid");
  expect(colours.ringWidth).toBeGreaterThanOrEqual(2);
  expect(contrast(colours.ring, colours.behind)).toBeGreaterThanOrEqual(3);
}

for (const theme of ["light", "dark"] as const) {
  test(`home search has a visible border and focus ring in ${theme} mode`, async ({
    page,
  }) => {
    await page.goto("/");
    await useTheme(page, theme);
    const box = page.locator(".home-page .search-controls");
    await expectVisibleBorder(box);

    await box.getByRole("searchbox").focus();
    await expectVisibleBorder(box);
    await expectVisibleRing(box);
  });

  test(`open header search has a visible focus ring and border in ${theme} mode`, async ({
    page,
  }) => {
    await page.goto("/blog");
    await useTheme(page, theme);
    await page.getByRole("button", { name: "Search players" }).click();
    const input = page.getByRole("searchbox", {
      name: "Search players and Clash Lens profiles",
    });
    await expect(input).toBeFocused();
    const box = page.locator(".header-search-panel .search-controls");

    await expectVisibleBorder(box);
    await expectVisibleRing(box);
    await expectNoSeriousAccessibilityViolations(page);

    // Moving into the suggestions drops the ring, so the border alone must stay visible.
    await input.fill("Synthetic Clasher 001");
    const suggestion = page
      .locator(".header-search-panel")
      .getByTestId("search-suggestion")
      .first();
    await suggestion.focus();
    await expect(suggestion).toBeFocused();
    await expectVisibleBorder(box);
  });

  test(`private group Add player box has a visible border in ${theme} mode`, async ({
    page,
  }) => {
    await signIn(page);
    await ensureAccount(page, "lensscout", "Lens Scout");
    await page.goto("/account/watchlists");
    if (await page.getByRole("heading", { name: "No watchlists yet" }).isVisible()) {
      await page.getByLabel("Watchlist name").first().fill("War plan");
      await page.getByRole("button", { name: "Create watchlist" }).click();
      await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();
    }
    await useTheme(page, theme);
    // Add player waits in the first card's ⋯ menu.
    await page.locator(".group-more > summary").first().click();
    await page.getByRole("link", { name: "Add player" }).first().click();
    const input = page.getByLabel("Player tag").first();
    await expectVisibleBorder(input);

    await input.focus();
    await expectVisibleBorder(input);
    await expectVisibleRing(input);
  });
}
