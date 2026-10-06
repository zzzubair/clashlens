import type { Config } from "@react-router/dev/config";

export default {
  ssr: true,
  // Send the whole route list with the page. Looking routes up while browsing
  // added a request that, when a phone's connection dropped, failed the page.
  routeDiscovery: { mode: "initial" },
} satisfies Config;
