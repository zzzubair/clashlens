/**
 * Server-only website configuration for Google login.
 *
 * Configuration is read from the environment exactly once. Startup fails with
 * a safe WebsiteConfigError when production login is enabled but a required
 * value is missing or malformed. Error messages never echo secret values or
 * environment contents. The Google issuer override is honored only outside
 * production; production rejects it outright so tests can never leak into
 * live traffic.
 */

import { readFileSync } from "node:fs";

import { decodeSecretValue, loadSecretFile } from "./signer.server";

export const GOOGLE_ISSUER = "https://accounts.google.com";
export const MAX_GOOGLE_CLIENT_ID_LENGTH = 256;
export const MAX_CLIENT_SECRET_LENGTH = 512;

export class WebsiteConfigError extends Error {
  constructor(reason: string) {
    super(`website login configuration is invalid: ${reason}`);
    this.name = "WebsiteConfigError";
  }
}

export interface WebsiteConfig {
  production: boolean;
  loginEnabled: boolean;
  /** Exact public origin: scheme, host, port, no path, query, or fragment. */
  publicOrigin: URL;
  googleClientId: string;
  googleClientSecret: string;
  /** Exactly 32 bytes; HMAC key for the login and OAuth transaction cookies. */
  loginSecret: Buffer;
  /** OpenID issuer for discovery; defaults to Google, overridden in tests. */
  googleIssuerUrl: URL;
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
      loginSecret: Buffer.alloc(0),
      googleIssuerUrl: new URL(GOOGLE_ISSUER),
      cookieSecure,
    };
  }
  const googleClientId = loadGoogleClientId(env.CLASHLENS_GOOGLE_CLIENT_ID);
  const googleClientSecret = loadGoogleClientSecret(
    env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE,
    env.CLASHLENS_GOOGLE_CLIENT_SECRET,
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
  return {
    production,
    loginEnabled,
    publicOrigin,
    googleClientId,
    googleClientSecret,
    loginSecret,
    googleIssuerUrl,
    cookieSecure,
  };
}

function loadPublicOrigin(raw: string | undefined, production: boolean): URL {
  if (raw === undefined || raw.trim() === "") {
    throw new WebsiteConfigError("missing public origin");
  }
  let origin: URL;
  try {
    origin = new URL(raw.trim());
  } catch {
    throw new WebsiteConfigError("malformed public origin");
  }
  if (origin.protocol !== "https:" && origin.protocol !== "http:") {
    throw new WebsiteConfigError("public origin must use http or https");
  }
  if (origin.pathname !== "/" || origin.search !== "" || origin.hash !== "") {
    throw new WebsiteConfigError(
      "public origin must not carry a path, query, or fragment",
    );
  }
  if (origin.protocol === "http:") {
    if (production) {
      throw new WebsiteConfigError("production requires an https public origin");
    }
    if (!isLocalHostname(origin.hostname)) {
      throw new WebsiteConfigError(
        "an http public origin is allowed only for explicit local test origins",
      );
    }
  }
  return origin;
}

function isLocalHostname(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
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
