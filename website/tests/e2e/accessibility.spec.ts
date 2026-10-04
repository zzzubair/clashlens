import { expect, type Page, test } from "@playwright/test";

import { expectNoSeriousAccessibilityViolations } from "./helpers/account";

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

// Paints the search box's ring and border onto the panel behind them.
async function headerSearchBox(page: Page) {
  return page.evaluate(() => {
    const style = getComputedStyle(
      document.querySelector(".header-search-panel .search-controls")!,
    );
    const background = getComputedStyle(
      document.querySelector(".header-search-panel")!,
    ).backgroundColor;
    // Let the browser blend a see-through colour onto the panel it sits on.
    const paint = (colour: string) => {
      const context = document.createElement("canvas").getContext("2d")!;
      context.fillStyle = background;
      context.fillRect(0, 0, 1, 1);
      context.fillStyle = colour;
      context.fillRect(0, 0, 1, 1);
      return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
    };
    return {
      style: style.outlineStyle,
      width: parseFloat(style.outlineWidth),
      colour: paint(style.outlineColor),
      border: paint(style.borderTopColor),
      background: paint(background),
    };
  });
}

for (const theme of ["light", "dark"] as const) {
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

    const focused = await headerSearchBox(page);
    expect(focused.style).toBe("solid");
    expect(focused.width).toBeGreaterThanOrEqual(2);
    expect(contrast(focused.colour, focused.background)).toBeGreaterThanOrEqual(3);
    expect(contrast(focused.border, focused.background)).toBeGreaterThanOrEqual(3);
    await expectNoSeriousAccessibilityViolations(page);

    // Moving into the suggestions drops the ring, so the border alone must stay visible.
    await input.fill("Synthetic Clasher 001");
    const suggestion = page
      .locator(".header-search-panel")
      .getByTestId("search-suggestion")
      .first();
    await suggestion.focus();
    await expect(suggestion).toBeFocused();
    const unfocused = await headerSearchBox(page);
    expect(contrast(unfocused.border, unfocused.background)).toBeGreaterThanOrEqual(3);
  });
}
