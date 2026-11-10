import { afterEach, beforeEach, expect, it, vi } from "vitest";
import DOMAIN from "../../../testdata/w6-domain-contract.json";
import { mountOverlay } from "../../app/lib/obs-overlay-view";

const ORIGIN = "https://clashlens.example";
let stop: (() => void) | undefined;
let message: (event: { origin: string; data: unknown }) => void;
const elements = () => ({
  innerHTML: "",
  style: {},
  firstElementChild: { offsetWidth: 366, offsetHeight: 222, style: {} },
  querySelector: () => null,
});
let stage: ReturnType<typeof elements>;
const fetchMock = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock.mockReset();
  stage = elements();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("location", { origin: ORIGIN });
  vi.stubGlobal("document", {
    fonts: { ready: Promise.resolve(), load: () => Promise.resolve() },
  });
  vi.stubGlobal("window", {
    parent: { postMessage: vi.fn() },
    addEventListener: (_type: string, listener: typeof message) => {
      message = listener;
    },
    removeEventListener: vi.fn(),
  });
});

afterEach(() => {
  stop?.();
  stop = undefined;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function mount(link: string | null) {
  const root = { innerHTML: "", querySelector: () => stage } as unknown as HTMLElement;
  stop = mountOverlay(root, { link, search: "?p=2PP&w=366&h=222" });
}

it.each([false, true])(
  "starts a preview on its first settings message, demo=%s",
  async (demo) => {
    fetchMock.mockResolvedValue(
      Response.json({ state: "legend", name: "Nova", trophies: 5300 }),
    );
    mount(null);
    expect(fetchMock).not.toHaveBeenCalled();
    message({
      origin: ORIGIN,
      data: {
        type: "cl-overlay-settings",
        contract: DOMAIN.overlay,
        search: `?p=2PP&w=366&h=222${demo ? "&demo=1" : ""}`,
      },
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(stage.innerHTML).toContain("Nova");
    await vi.advanceTimersByTimeAsync(demo ? 800 : 20_000);
    if (demo) expect(stage.innerHTML).toContain("Net today");
    expect(fetchMock).toHaveBeenCalledTimes(demo ? 1 : 2);
  },
);

it("keeps a retired link blank and stops polling after a late player reply", async () => {
  let finish!: (reply: Response) => void;
  fetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/resources/"))
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    return fetchMock.mock.calls.length === 1
      ? Response.json({
          version: 1,
          tag: "#2PP",
          settings: { w: "366", h: "222" },
          settingsContract: DOMAIN.overlay,
        })
      : new Response(null, { status: 404 });
  });
  mount("saved-link");
  await vi.advanceTimersByTimeAsync(0);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(3_000);
  finish(Response.json({ state: "legend", name: "Late player" }));
  await vi.advanceTimersByTimeAsync(0);
  expect(stage.innerHTML).toBe("");
  await vi.advanceTimersByTimeAsync(59_999);
  expect(fetchMock).toHaveBeenCalledTimes(3);
  await vi.advanceTimersByTimeAsync(1);
  expect(fetchMock).toHaveBeenCalledTimes(4);
});
