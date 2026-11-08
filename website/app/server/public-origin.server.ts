/** Public address settings validate independently of login and API credentials. */
let cachedPublicOrigin: URL | undefined;

export class WebsiteConfigError extends Error {
  constructor(reason: string) {
    super(`website login configuration is invalid: ${reason}`);
    this.name = "WebsiteConfigError";
  }
}

/** Canonical addresses do not require any provider credentials. */
export function getPublicOrigin(): URL {
  return (cachedPublicOrigin ??= loadPublicOrigin(
    process.env.CLASHLENS_PUBLIC_ORIGIN,
    process.env.NODE_ENV === "production",
  ));
}

export function loadPublicOrigin(raw: string | undefined, production: boolean): URL {
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

export function isLocalHostname(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}
