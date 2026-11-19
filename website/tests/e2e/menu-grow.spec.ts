import { expect, test } from "@playwright/test";

import { blogFixtureOrigin } from "../fixtures/test-values";

// A trace's page snapshots restyle closed menus, hiding a grow that fails to replay.
test.use({
  baseURL: blogFixtureOrigin,
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
  trace: "off",
});

test("on a phone, More grows open again on every open, not only the first", async ({
  page,
}) => {
  await page.goto("/blog");
  await page.waitForLoadState("networkidle");
  const more = page.locator("details.site-menu");
  // The faintest the menu looks over the frames drawn just after More is tapped.
  const faintest = () =>
    page.evaluate(
      () =>
        new Promise<number>((resolve) => {
          document.querySelector<HTMLElement>("details.site-menu > summary")!.click();
          const panel = document.querySelector(".site-menu-panel")!;
          const start = performance.now();
          let least = 1;
          const frame = () => {
            least = Math.min(least, Number(getComputedStyle(panel).opacity));
            if (performance.now() - start < 120) requestAnimationFrame(frame);
            else resolve(least);
          };
          requestAnimationFrame(frame);
        }),
    );
  for (let open = 1; open <= 3; open++) {
    expect(await faintest(), `open ${open}`).toBeLessThan(0.5);
    await more.locator("summary").click();
    await expect(more).not.toHaveAttribute("open");
    // Let the grow finish, so the next one is a fresh start.
    await page.waitForTimeout(300);
  }
});
