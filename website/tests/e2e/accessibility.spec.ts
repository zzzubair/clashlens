import { expect, type Locator, type Page, test } from "@playwright/test";

import {
  ensureAccount,
  expectNoSeriousAccessibilityViolations,
  signIn,
} from "./helpers/account";

for (const [name, path, heading] of [
  ["home", "/", "Legend League"],
  ["player", "/players/%232PP", "Synthetic Clasher 001"],
  ["leaderboard", "/leaderboards/tracked", "Live Leaderboard"],
  ["login", "/login", "Sign in"],
  ["about", "/about", "About Clash Lens"],
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
async function boxColours(box: Locator) {
  return box.evaluate((element) => {
    const style = getComputedStyle(element);
    let backdrop = element.parentElement!;
    while (getComputedStyle(backdrop).backgroundColor === "rgba(0, 0, 0, 0)") {
      backdrop = backdrop.parentElement!;
    }
    const paint = (under: string, colour: string) => {
      const context = document.createElement("canvas").getContext("2d")!;
      context.fillStyle = under;
      context.fillRect(0, 0, 1, 1);
      context.fillStyle = colour;
      context.fillRect(0, 0, 1, 1);
      return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
    };
    const behind = getComputedStyle(backdrop).backgroundColor;
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
    await page.goto("/about");
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
    await page.goto("/account/groups");
    if (await page.getByRole("heading", { name: "No private groups yet" }).isVisible()) {
      await page.getByLabel("Group name").first().fill("War plan");
      await page.getByRole("button", { name: "Create group" }).click();
      await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();
    }
    await useTheme(page, theme);
    await page.getByRole("link", { name: "Add player" }).first().click();
    const input = page.getByLabel("Player tag").first();
    await expectVisibleBorder(input);

    await input.focus();
    await expectVisibleBorder(input);
    await expectVisibleRing(input);
  });
}
