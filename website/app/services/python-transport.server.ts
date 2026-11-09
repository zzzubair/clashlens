import type { LoginIdentity } from "../server/auth.server";
import {
  createProofHeaders,
  decodeSecretValue,
  loadSecretFile,
} from "../server/signer.server";
import { PythonApiError, isRecord } from "./python-response.server";

const DEFAULT_CALLER = "typescript-website";
const DEFAULT_KEY_ID = "current";
export const REQUEST_TIMEOUT_MS = 5_000;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;

interface ClientConfig {
  baseUrl: URL;
  caller: string;
  keyId: string;
  key: Buffer;
}
// Private API settings load on first use, so public pages survive an API misconfiguration.
// A successful validation is kept for this server process.
let cachedConfig: ClientConfig | undefined;

export interface PythonRequestOptions {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  body?: Buffer;
  expectedKind?: string | string[];
  requestId?: string;
  identity?: LoginIdentity;
  allowedStatuses?: readonly number[];
  timeoutMs?: number;
}

export async function requestJson<T>(
  target: string,
  options: PythonRequestOptions,
): Promise<T> {
  const { expectedKind, allowedStatuses } = options;
  const { status, payload } = await requestJsonRaw(target, options);
  if (!(status >= 200 && status < 300) && !allowedStatuses?.includes(status)) {
    throw new PythonApiError(status, payload);
  }
  if (expectedKind === undefined) return payload as T;
  const acceptedKinds = Array.isArray(expectedKind) ? expectedKind : [expectedKind];
  if (
    !isRecord(payload) ||
    typeof payload.kind !== "string" ||
    !acceptedKinds.includes(payload.kind)
  ) {
    throw new PythonApiError(502, { error: "unavailable" });
  }
  return payload as T;
}

export async function requestJsonRaw(
  target: string,
  {
    method,
    body,
    requestId,
    identity,
    timeoutMs = REQUEST_TIMEOUT_MS,
  }: PythonRequestOptions,
): Promise<{ status: number; payload: unknown }> {
  const config = getConfig();
  const proof = createProofHeaders({
    key: config.key,
    caller: config.caller,
    keyId: config.keyId,
    method,
    rawTarget: target,
    body,
    provider: identity?.provider,
    providerSubject: identity?.providerSubject,
    requestId,
    lifetimeSeconds: 10,
  });
  let response: Response | undefined;
  let payload: unknown;
  try {
    response = await fetch(new URL(target, config.baseUrl), {
      method,
      headers: {
        ...proof.headers,
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      body: body === undefined ? undefined : Uint8Array.from(body),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    payload = await readPayload(response);
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "TimeoutError") {
      throw new PythonApiError(503, { error: "timeout" });
    }
    throw response === undefined
      ? new PythonApiError(503, { error: "unavailable" })
      : new PythonApiError(502, { error: "malformed" });
  }
  return { status: response.status, payload };
}

async function readPayload(response: Response): Promise<unknown> {
  const contentLength = response.headers.get("content-length");
  if (
    contentLength !== null &&
    (!/^\d+$/.test(contentLength) || Number(contentLength) > MAX_RESPONSE_BYTES)
  ) {
    return { error: "malformed" };
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength > MAX_RESPONSE_BYTES) return { error: "malformed" };
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return { error: "malformed" };
  }
  if (text === "") return {};
  try {
    return JSON.parse(text);
  } catch {
    return { error: "malformed" };
  }
}

function getConfig(): ClientConfig {
  if (cachedConfig !== undefined) return cachedConfig;
  const rawBaseUrl = process.env.CLASHLENS_PYTHON_API_URL?.trim();
  const configuredCaller = process.env.CLASHLENS_PYTHON_HMAC_CALLER?.trim();
  const configuredKeyId = process.env.CLASHLENS_PYTHON_HMAC_KEY_ID?.trim();
  const production = process.env.NODE_ENV === "production";
  if (production && (!configuredCaller || !configuredKeyId)) {
    throw new PythonApiError(503, { error: "unavailable" });
  }
  const caller = configuredCaller || DEFAULT_CALLER;
  const keyId = configuredKeyId || DEFAULT_KEY_ID;
  if (!rawBaseUrl) throw new PythonApiError(503, { error: "unavailable" });
  let baseUrl: URL;
  try {
    baseUrl = new URL(rawBaseUrl);
    if (
      !/^https?:$/.test(baseUrl.protocol) ||
      baseUrl.pathname !== "/" ||
      baseUrl.search ||
      baseUrl.hash
    ) {
      throw new Error("invalid base URL");
    }
  } catch {
    throw new PythonApiError(503, { error: "unavailable" });
  }
  let key: Buffer;
  try {
    const secretFile = process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE?.trim();
    const testSecret = process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64;
    if (secretFile) key = loadSecretFile(secretFile);
    else if (process.env.NODE_ENV !== "production" && testSecret) {
      key = decodeSecretValue(testSecret);
    } else throw new Error("missing private API secret");
  } catch {
    throw new PythonApiError(503, { error: "unavailable" });
  }
  cachedConfig = { baseUrl, caller, keyId, key };
  return cachedConfig;
}
