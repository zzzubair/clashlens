import { devices, expect, test, type Locator, type Page } from "@playwright/test";

// WebKit with an iPhone screen and a finger instead of a mouse.
test.use({ ...devices["iPhone 13"] });

// iPhone Safari never focuses a tapped link, so the search box loses focus to
// nothing. The Linux WebKit these tests run on does focus it, so act like Safari.
async function tapLikeSafari(page: Page) {
  await page.addInitScript(() => {
    window.addEventListener("mousedown", (event) => {
      if (event.defaultPrevented || !(event.target as Element).closest("a[href]")) return;
      event.preventDefault();
      (document.activeElement as HTMLElement | null)?.blur();
    });
  });
}

async function tapSuggestion(page: Page, input: Locator) {
  const suggestion = page.getByTestId("search-suggestion").first();
  // Typing before the page has started up is lost, so type again until it lands.
  await expect(async () => {
    await input.fill("Synthetic Clasher 00");
    await expect(suggestion).toBeVisible({ timeout: 2_000 });
  }).toPass();
  const href = (await suggestion.getAttribute("href"))!;
  await suggestion.tap();
  await expect(page).toHaveURL(href);
}

test("tapping a home search suggestion opens that player", async ({ page }) => {
  await tapLikeSafari(page);
  await page.goto("/");
  const input = page.getByRole("searchbox", {
    name: "Search players and Clash Lens profiles",
  });
  await input.tap();
  await tapSuggestion(page, input);
});

test("tapping a header search suggestion opens that player", async ({ page }) => {
  await tapLikeSafari(page);
  await page.goto("/about");
  const input = page.getByRole("searchbox", {
    name: "Search players and Clash Lens profiles",
  });
  await expect(async () => {
    if (!(await input.isVisible()))
      await page.getByRole("button", { name: "Search players" }).tap();
    await expect(input).toBeFocused({ timeout: 1_000 });
  }).toPass();
  await tapSuggestion(page, input);
});
