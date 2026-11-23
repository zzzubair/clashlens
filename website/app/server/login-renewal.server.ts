import {
  buildSetCookieHeader,
  createLoginCookieValue,
  createLoginSessionBinding,
  LOGIN_COOKIE_LIFETIME_SECONDS,
  LOGIN_COOKIE_NAME,
  LOGIN_COOKIE_RENEW_AFTER_SECONDS,
  parseLoginCookieValue,
} from "./auth-cookies.server";
import type { LoginSession } from "./auth-cookies.server";
import { parseCookieHeader } from "./actions.server";
import { getWebsiteConfig } from "./config.server";
import type { WebsiteConfig } from "./config.server";
import { isLoginRevoked } from "./login-session.server";

const RENEWAL_CHECK_TIMEOUT_MS = 1_000;

export type LoginCookieStage =
  | { kind: "absent" }
  | { kind: "unusable" }
  | { kind: "recent"; session: LoginSession }
  | { kind: "due"; session: LoginSession; value: string };

export function loginCookieStage(
  request: Request,
  config: WebsiteConfig,
  nowMs: number,
): LoginCookieStage {
  if (!config.loginEnabled || config.loginSecret.length !== 32) return { kind: "absent" };
  const value = parseCookieHeader(request.headers.get("cookie")).get(LOGIN_COOKIE_NAME);
  if (value === undefined) return { kind: "absent" };
  const session = parseLoginCookieValue(
    value,
    config.loginSecret,
    Math.floor(nowMs / 1000),
  );
  if (session === null) return { kind: "unusable" };
  if (nowMs - session.issuedAtMs < LOGIN_COOKIE_RENEW_AFTER_SECONDS * 1000) {
    return { kind: "recent", session };
  }
  return { kind: "due", session, value };
}

export async function renewLoginCookie(
  request: Request,
  next: () => Promise<Response>,
): Promise<Response> {
  const renewal = renewedLoginCookie(request);
  const response = await next();
  const cookie = await renewal;
  const directives = (response.headers.get("Cache-Control") ?? "")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .split(",")
    .map((directive) => directive.trim().toLowerCase());
  if (
    cookie === null ||
    !directives.some(
      (directive) => directive === "no-store" || directive === "private",
    ) ||
    directives.some((directive) => directive.split("=")[0]?.trim() === "public") ||
    response.headers
      .getSetCookie()
      .some((header) => header.startsWith(`${LOGIN_COOKIE_NAME}=`))
  ) {
    return response;
  }
  const headers = new Headers(response.headers);
  headers.append("Set-Cookie", cookie);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function renewedLoginCookie(request: Request): Promise<string | null> {
  try {
    const config = getWebsiteConfig();
    const nowMs = Date.now();
    const stage = loginCookieStage(request, config, nowMs);
    if (
      stage.kind !== "due" ||
      (await isLoginRevoked(stage.session, stage.value, RENEWAL_CHECK_TIMEOUT_MS))
    ) {
      return null;
    }
    return buildSetCookieHeader(
      LOGIN_COOKIE_NAME,
      createLoginCookieValue(
        {
          provider: stage.session.provider,
          providerSubject: stage.session.providerSubject,
        },
        config.loginSecret,
        nowMs,
        {
          sessionBinding: createLoginSessionBinding(stage.value),
          signedInAtMs: stage.session.signedInAtMs,
        },
      ),
      LOGIN_COOKIE_LIFETIME_SECONDS,
      config.cookieSecure,
    );
  } catch {
    return null;
  }
}
