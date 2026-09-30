import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { request as httpRequest } from "node:http";
import type { Server } from "node:http";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ServerBuild } from "react-router";
import { createWebsiteServer } from "../../server";
import { createClientAddressContext } from "../../app/server/client-address.server";
import { clearPublicRefreshLimits } from "../../app/server/abuse.server";
import { action } from "../../app/routes/refresh";

vi.mock("../../app/services/python.server", () => ({
  createPythonClient: () => ({ requestRefresh: async () => ({ state: "queued" }) }),
}));
vi.mock("../../app/server/config.server", () => ({
  getWebsiteConfig: () => ({ publicOrigin: new URL("https://clashlens.example") }),
}));

let directory: string;
let server: Server;
let port: number;

beforeEach(async () => {
  clearPublicRefreshLimits();
  directory = await mkdtemp(join(process.cwd(), "node_modules/.website-server-test-"));
  await mkdir(join(directory, "assets"));
  await writeFile(join(directory, "assets", "app.js"), "window.example = true;");
  await writeFile(join(directory, "site.webmanifest"), '{"name":"Clash Lens"}');
  await writeFile(join(directory, ".secret"), "must not be served");
  // A resource route needs no rendered UI. Use the real React Router Node
  // adapter and Refresh action so the socket-to-action boundary is exercised.
  const build = {
    entry: {
      module: {
        default: () => new Response("Not found", { status: 404 }),
        getLoadContext: createClientAddressContext({
          CLASHLENS_TRUSTED_PROXY_IP: "127.0.0.2",
        }),
      },
    },
    routes: {
      refresh: {
        id: "refresh",
        path: "resources/players/:tag/refresh",
        module: { action },
      },
    },
    assets: { version: "test", routes: {}, entry: { module: "" }, url: "" },
    future: {},
    ssr: true,
    isSpaMode: false,
    prerender: [],
    publicPath: "/",
    assetsBuildDirectory: directory,
    routeDiscovery: { mode: "initial", manifestPath: "/__manifest" },
  } as unknown as ServerBuild;
  server = await createWebsiteServer(build, directory);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing test port");
  port = address.port;
});

afterEach(async () => {
  if (server)
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  if (directory) await rm(directory, { recursive: true, force: true });
});

function refresh(peer: string, headers: Record<string, string>) {
  return new Promise<number | undefined>((resolve, reject) => {
    const request = httpRequest(
      {
        hostname: "127.0.0.1",
        port,
        localAddress: peer,
        path: "/resources/players/%232PP/refresh",
        method: "POST",
        headers: {
          Origin: "https://clashlens.example",
          "Content-Type": "application/x-www-form-urlencoded",
          ...headers,
        },
      },
      (response) => {
        response.resume();
        response.on("end", () => resolve(response.statusCode));
      },
    );
    request.on("error", reject);
    request.end("idempotencyKey=11111111-1111-4111-8111-111111111111");
  });
}

it("uses the real socket peer and rejects forged headers on a direct HTTP connection", async () => {
  for (let index = 0; index < 7; index++) {
    expect(
      await refresh("127.0.0.1", {
        "X-Forwarded-For": `203.0.113.${index + 1}`,
        Forwarded: `for=203.0.113.${index + 1}`,
        "X-Real-IP": `203.0.113.${index + 1}`,
        "CF-Connecting-IP": `203.0.113.${index + 1}`,
      }),
    ).toBe(index < 6 ? 202 : 429);
  }
  // A different socket peer still has its own allowance.
  expect(await refresh("127.0.0.3", {})).toBe(202);
});

it("accepts the visitor header only on the configured proxy connection", async () => {
  for (let index = 0; index < 7; index++) {
    expect(
      await refresh("127.0.0.2", {
        "CF-Connecting-IP": "198.51.100.1",
        "X-Forwarded-For": `203.0.113.${index + 1}`,
      }),
    ).toBe(index < 6 ? 202 : 429);
  }
  expect(await refresh("127.0.0.2", { "CF-Connecting-IP": "198.51.100.2" })).toBe(202);
});

it("serves built assets with caching, compression and HEAD without exposing other files", async () => {
  const url = `http://127.0.0.1:${port}`;
  const asset = await fetch(`${url}/assets/app.js`);
  expect(asset.status).toBe(200);
  expect(asset.headers.get("content-type")).toBe("text/javascript");
  expect(asset.headers.get("cache-control")).toContain("immutable");
  expect(asset.headers.get("content-encoding")).toBe("gzip");
  expect(await asset.text()).toBe("window.example = true;");
  const uncompressed = await fetch(`${url}/assets/app.js`, {
    headers: { "Accept-Encoding": "gzip;q=0.0, identity" },
  });
  expect(uncompressed.headers.get("content-encoding")).toBeNull();
  expect(await uncompressed.text()).toBe("window.example = true;");
  const cached = await fetch(`${url}/assets/app.js`, {
    headers: { "If-None-Match": asset.headers.get("etag")! },
  });
  expect(cached.status).toBe(304);
  const head = await fetch(`${url}/assets/app.js`, { method: "HEAD" });
  expect(head.status).toBe(200);
  expect(await head.text()).toBe("");
  const manifest = await fetch(`${url}/site.webmanifest`);
  expect(manifest.headers.get("content-type")).toBe("application/manifest+json");
  expect(await manifest.json()).toEqual({ name: "Clash Lens" });
  for (const path of ["/.secret", "/%2e%2e/package.json", "/assets/../../package.json"]) {
    expect((await fetch(`${url}${path}`)).status).toBe(404);
  }
});
