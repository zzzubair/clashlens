/** Run both loopback login providers as one local fixture service. */
import "./discord-provider.ts";
import "./oidc-provider.ts";

process.once("SIGINT", () => process.exit(0));
