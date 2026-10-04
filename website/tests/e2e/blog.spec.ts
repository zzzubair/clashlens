import { expect, test, type Locator } from "@playwright/test";

import { blogFixtureOrigin } from "../fixtures/test-values";

// This server reads tests/fixtures/blog and has login turned off, so every
// visitor is signed out.
test.use({ baseURL: blogFixtureOrigin });

// The post body is rebuilt when the page's scripts start, so retry until the
// image in place has loaded.
async function loadedWidth(image: Locator) {
  await image.scrollIntoViewIfNeeded({ timeout: 1_000 }).catch(() => {});
  return image.evaluate((element: HTMLImageElement) => element.naturalWidth);
}

test("a draft is hidden from the list, its address and the feed when signed out", async ({
  page,
  request,
}) => {
  await page.goto("/blog");
  await expect(
    page.getByRole("link", { name: "How Legend League matchmaking works" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Draft preview" })).toHaveCount(0);

  const response = await page.goto("/blog/draft-preview");
  expect(response?.status()).toBe(404);
  await expect(page.getByText("This draft must stay hidden")).toHaveCount(0);

  const feed = await request.get("/blog/rss.xml");
  expect(feed.ok()).toBe(true);
  expect(await feed.text()).not.toContain("draft-preview");
});

test("a chart shows the version matching the site theme and skips the other", async ({
  page,
}) => {
  const requested: string[] = [];
  page.on("request", (sent) => requested.push(new URL(sent.url()).pathname));
  await page.goto("/blog/how-matchmaking-works");
  const light = page.locator("img.blog-img-light");
  const dark = page.locator("img.blog-img-dark");
  await expect.poll(() => loadedWidth(light)).toBe(1);
  await expect(light).toBeVisible();
  await expect(dark).toBeHidden();
  expect(requested).toContain("/blog/media/gap-chart.png");
  expect(requested).not.toContain("/blog/media/gap-chart-dark.png");

  // Save the choice like the theme toggle does; the page resets an unsaved theme.
  await page.evaluate(() => localStorage.setItem("clashlens-theme", "dark"));
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect.poll(() => loadedWidth(dark)).toBe(1);
  await expect(dark).toBeVisible();
  await expect(light).toBeHidden();
  await expect(dark).toHaveAttribute("alt", "Trophy gap by band");
});
