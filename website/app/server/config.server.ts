import {
  WebsiteConfigError,
  isLocalHostname,
  loadPublicOrigin,
} from "./public-origin.server";
/**
 * Login credentials and the public origin cache successful validation for this process.
 * Private API settings validate separately on first use, preserving public-page availability.
 * Feature switches and analytics settings read on demand; trusted proxy settings are
 * captured when HTTP context is created. Blog directory/owner settings read on demand,
 * with folder contents refreshed by the blog cache. Secret errors never echo values.
 */

import { readFileSync } from "node:fs";

import type { AnalyticsSettings } from "../lib/analytics";
import { decodeSecretValue, loadSecretFile } from "./signer.server";

export const GOOGLE_ISSUER = "https://accounts.google.com";
export const DISCORD_API_BASE_URL = "https://discord.com";
export const MAX_GOOGLE_CLIENT_ID_LENGTH = 256;
/** Discord application (user) IDs are fixed-width numeric snowflakes. */
export const MAX_DISCORD_CLIENT_ID_LENGTH = 20;
export const MAX_CLIENT_SECRET_LENGTH = 512;

export interface WebsiteConfig {
  production: boolean;
  loginEnabled: boolean;
  /** Exact public origin: scheme, host, port, no path, query, or fragment. */
  publicOrigin: URL;
  googleClientId: string;
  googleClientSecret: string;
  discordClientId: string;
  discordClientSecret: string;
  /** Exactly 32 bytes; HMAC key for the login and OAuth transaction cookies. */
  loginSecret: Buffer;
  /** OpenID issuer for discovery; defaults to Google, overridden in tests. */
  googleIssuerUrl: URL;
  /** Discord API base; defaults to Discord, overridden in tests only. */
  discordApiBaseUrl: URL;
  /** True for any https public origin; cookies then carry the Secure flag. */
  cookieSecure: boolean;
}

let cachedConfig: WebsiteConfig | undefined;

export function getWebsiteConfig(): WebsiteConfig {
  if (cachedConfig === undefined) cachedConfig = loadWebsiteConfig(process.env);
  return cachedConfig;
}

export function isWebsiteLoginEnabled(
  env: Record<string, string | undefined> = process.env,
): boolean {
  return env.CLASHLENS_LOGIN_ENABLED === "true" || env.CLASHLENS_LOGIN_ENABLED === "1";
}

/** The dashboard stays hidden, as an unknown page, until this is "true". */
export function isDashboardEnabled(
  env: Record<string, string | undefined> = process.env,
): boolean {
  return env.CLASHLENS_DASHBOARD_ENABLED === "true";
}

/** Crews stay hidden, as an unknown page, until this is "true". */
export function isCrewsEnabled(
  env: Record<string, string | undefined> = process.env,
): boolean {
  return env.CLASHLENS_CREWS_ENABLED === "true";
}

/**
 * Visitor counting stays off, with no script and no requests, unless the
 * PostHog project token is set. The token is public by design: it can only
 * send events.
 */
export function loadAnalyticsSettings(
  env: Record<string, string | undefined> = process.env,
): AnalyticsSettings | null {
  const token = env.CLASHLENS_POSTHOG_TOKEN?.trim() ?? "";
  return /^phc_[A-Za-z0-9]{20,64}$/.test(token) ? { token } : null;
}

export function loadWebsiteConfig(
  env: Record<string, string | undefined> = process.env,
): WebsiteConfig {
  const production = env.NODE_ENV === "production";
  const loginEnabled = isWebsiteLoginEnabled(env);
  const publicOrigin = loadPublicOrigin(env.CLASHLENS_PUBLIC_ORIGIN, production);
  const cookieSecure = publicOrigin.protocol === "https:";
  if (!loginEnabled) {
    return {
      production,
      loginEnabled,
      publicOrigin,
      googleClientId: "",
      googleClientSecret: "",
      discordClientId: "",
      discordClientSecret: "",
      loginSecret: Buffer.alloc(0),
      googleIssuerUrl: new URL(GOOGLE_ISSUER),
      discordApiBaseUrl: new URL(DISCORD_API_BASE_URL),
      cookieSecure,
    };
  }
  const googleClientId = loadGoogleClientId(env.CLASHLENS_GOOGLE_CLIENT_ID);
  const googleClientSecret = loadGoogleClientSecret(
    env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE,
    env.CLASHLENS_GOOGLE_CLIENT_SECRET,
    production,
  );
  // Phase 1 production login requires both providers. A partial
  // configuration must fail before the website listens.
  const discordClientId = loadDiscordClientId(env.CLASHLENS_DISCORD_CLIENT_ID);
  const discordClientSecret = loadDiscordClientSecret(
    env.CLASHLENS_DISCORD_CLIENT_SECRET_FILE,
    env.CLASHLENS_DISCORD_CLIENT_SECRET,
    production,
  );
  const loginSecret = loadLoginSecret(
    env.CLASHLENS_LOGIN_SECRET_FILE,
    env.CLASHLENS_LOGIN_SECRET_B64,
    production,
  );
  const googleIssuerUrl = loadGoogleIssuerUrl(
    env.CLASHLENS_GOOGLE_ISSUER_URL,
    production,
  );
  const discordApiBaseUrl = loadDiscordApiBaseUrl(
    env.CLASHLENS_DISCORD_API_BASE_URL,
    production,
  );
  return {
    production,
    loginEnabled,
    publicOrigin,
    googleClientId,
    googleClientSecret,
    discordClientId,
    discordClientSecret,
    loginSecret,
    googleIssuerUrl,
    discordApiBaseUrl,
    cookieSecure,
  };
}

function loadGoogleClientId(raw: string | undefined): string {
  if (
    raw === undefined ||
    raw.length === 0 ||
    raw.length > MAX_GOOGLE_CLIENT_ID_LENGTH ||
    !/^[A-Za-z0-9._~-]+$/.test(raw)
  ) {
    throw new WebsiteConfigError("missing or malformed Google client ID");
  }
  return raw;
}

function loadGoogleClientSecret(
  secretFile: string | undefined,
  secretEnv: string | undefined,
  production: boolean,
): string {
  if (secretFile !== undefined && secretFile !== "") {
    return loadPlainSecretFile(secretFile);
  }
  if (!production && secretEnv !== undefined && secretEnv !== "") {
    return validatePlainSecret(secretEnv, "Google client secret");
  }
  throw new WebsiteConfigError(
    production
      ? "missing protected Google client-secret file"
      : "missing Google client secret or protected secret file",
  );
}

function loadDiscordClientId(raw: string | undefined): string {
  if (
    raw === undefined ||
    raw.length === 0 ||
    raw.length > MAX_DISCORD_CLIENT_ID_LENGTH ||
    !/^[0-9]{17,20}$/.test(raw)
  ) {
    throw new WebsiteConfigError("missing or malformed Discord client ID");
  }
  return raw;
}

function loadDiscordClientSecret(
  secretFile: string | undefined,
  secretEnv: string | undefined,
  production: boolean,
): string {
  if (secretFile !== undefined && secretFile !== "") {
    return loadPlainSecretFile(secretFile);
  }
  if (!production && secretEnv !== undefined && secretEnv !== "") {
    return validatePlainSecret(secretEnv, "Discord client secret");
  }
  throw new WebsiteConfigError(
    production
      ? "missing protected Discord client-secret file"
      : "missing Discord client secret or protected secret file",
  );
}

function loadDiscordApiBaseUrl(raw: string | undefined, production: boolean): URL {
  if (raw === undefined || raw.trim() === "") {
    return new URL(DISCORD_API_BASE_URL);
  }
  if (production) {
    throw new WebsiteConfigError(
      "the Discord API base override is allowed only outside production",
    );
  }
  let base: URL;
  try {
    base = new URL(raw.trim());
  } catch {
    throw new WebsiteConfigError("malformed Discord API base override");
  }
  if (base.protocol !== "https:" && base.protocol !== "http:") {
    throw new WebsiteConfigError("Discord API base override must use http or https");
  }
  if (base.search !== "" || base.hash !== "") {
    throw new WebsiteConfigError(
      "Discord API base override must not carry a query or fragment",
    );
  }
  if (base.protocol === "http:" && !isLocalHostname(base.hostname)) {
    throw new WebsiteConfigError(
      "an http Discord API base override is allowed only for explicit local test origins",
    );
  }
  return base;
}

function loadPlainSecretFile(path: string): string {
  let value: string;
  try {
    value = readFileSync(path, "utf8");
  } catch {
    throw new WebsiteConfigError("protected secret file is not readable");
  }
  return validatePlainSecret(value.replace(/\r?\n$/, ""), "protected secret file");
}

function validatePlainSecret(value: string, label: string): string {
  if (
    value.length === 0 ||
    value.length > MAX_CLIENT_SECRET_LENGTH ||
    [...value].some((character) => {
      const code = character.charCodeAt(0);
      return code <= 0x1f || code === 0x7f;
    })
  ) {
    throw new WebsiteConfigError(`malformed ${label}`);
  }
  return value;
}

function loadLoginSecret(
  secretFile: string | undefined,
  secretEnv: string | undefined,
  production: boolean,
): Buffer {
  try {
    if (secretFile !== undefined && secretFile !== "") {
      return loadSecretFile(secretFile);
    }
    if (!production && secretEnv !== undefined && secretEnv !== "") {
      return decodeSecretValue(secretEnv);
    }
  } catch {
    throw new WebsiteConfigError("login secret must decode to exactly 32 bytes");
  }
  throw new WebsiteConfigError(
    production
      ? "missing protected browser-login secret file"
      : "missing browser-login secret or protected secret file",
  );
}

function loadGoogleIssuerUrl(raw: string | undefined, production: boolean): URL {
  if (raw === undefined || raw.trim() === "") {
    return new URL(GOOGLE_ISSUER);
  }
  if (production) {
    throw new WebsiteConfigError(
      "the Google issuer override is allowed only outside production",
    );
  }
  let issuer: URL;
  try {
    issuer = new URL(raw.trim());
  } catch {
    throw new WebsiteConfigError("malformed Google issuer override");
  }
  if (issuer.protocol !== "https:" && issuer.protocol !== "http:") {
    throw new WebsiteConfigError("Google issuer override must use http or https");
  }
  if (issuer.search !== "" || issuer.hash !== "") {
    throw new WebsiteConfigError(
      "Google issuer override must not carry a query or fragment",
    );
  }
  if (issuer.protocol === "http:" && !isLocalHostname(issuer.hostname)) {
    throw new WebsiteConfigError(
      "an http Google issuer override is allowed only for explicit local test origins",
    );
  }
  return issuer;
}
