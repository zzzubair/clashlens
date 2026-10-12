import { useEffect, useState } from "react";

const STORAGE_KEY = "clashlens-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";
// The browser bar matches the dark top bar in each theme.
const BAR_COLOR = { light: "#1d1426", dark: "#0b0810" };
let themeFrame = 0;

// A saved choice wins; otherwise the device's light or dark setting. Runs before
// paint so the page never flashes the other theme.
export const themeInitialization = `(() => {
  let saved;
  try { saved = localStorage.getItem("${STORAGE_KEY}"); } catch {}
  const dark = saved === "dark" || saved === "light" ? saved === "dark" : matchMedia("${DARK_QUERY}").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "${BAR_COLOR.dark}" : "${BAR_COLOR.light}");
})();`;

export function ThemeToggle() {
  const [dark, setDark] = useState(false);

  useEffect(() => {
    const media = window.matchMedia(DARK_QUERY);
    const sync = () => {
      let saved: string | null = null;
      try {
        saved = localStorage.getItem(STORAGE_KEY);
      } catch {
        /* Storage may be disabled. */
      }
      const next =
        saved === "dark" || saved === "light" ? saved === "dark" : media.matches;
      applyTheme(next);
      setDark(next);
    };
    sync();
    window.addEventListener("storage", sync);
    media.addEventListener("change", sync);
    return () => {
      window.removeEventListener("storage", sync);
      media.removeEventListener("change", sync);
    };
  }, []);

  return (
    <button
      type="button"
      className="theme-toggle"
      aria-label="Dark mode"
      aria-pressed={dark}
      title={dark ? "Switch to light mode" : "Switch to dark mode"}
      onClick={() => {
        const next = !dark;
        applyTheme(next);
        setDark(next);
        try {
          localStorage.setItem(STORAGE_KEY, next ? "dark" : "light");
        } catch {
          /* The current page still switches. */
        }
      }}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        width="20"
        height="20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {dark ? (
          <>
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" />
          </>
        ) : (
          <path d="M20.8 13.2A9 9 0 0 1 10.8 3.2 9 9 0 1 0 20.8 13.2Z" />
        )}
      </svg>
    </button>
  );
}

function applyTheme(dark: boolean) {
  const theme = dark ? "dark" : "light";
  if (document.documentElement.dataset.theme !== theme) {
    document.documentElement.classList.add("theme-changing");
    cancelAnimationFrame(themeFrame);
    themeFrame = requestAnimationFrame(() => {
      themeFrame = requestAnimationFrame(() => {
        document.documentElement.classList.remove("theme-changing");
      });
    });
  }
  document.documentElement.dataset.theme = theme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", BAR_COLOR[theme]);
}
