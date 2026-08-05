import { createHash, createHmac, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

import { isCanonicalUuid } from "../lib/validation";

export const PROOF_VERSION = "clashlens-hmac-v1";
export const AUDIENCE = "clashlens-python-private-api";

export interface SigningInput {
  proofVersion: string;
  callerB64Url: string;
  keyIdB64Url: string;
  audience: string;
  method: string;
  targetB64Url: string;
  bodySha256: string;
  issuedAt: string;
  expiresAt: string;
  requestId: string;
  providerB64Url: string;
  providerSubjectB64Url: string;
}

export interface ProofHeaders {
  "X-ClashLens-Proof-Version": string;
  "X-ClashLens-Caller": string;
  "X-ClashLens-Key-Id": string;
  "X-ClashLens-Issued-At": string;
  "X-ClashLens-Expires-At": string;
  "X-ClashLens-Request-Id": string;
  "X-ClashLens-Provider": string;
  "X-ClashLens-Provider-Subject": string;
  "X-ClashLens-Signature": string;
}

export interface ProofRequestOptions {
  key: Buffer;
  caller: string;
  keyId: string;
  method: string;
  rawTarget: string;
  body?: Buffer;
  provider?: string;
  providerSubject?: string;
  now?: number;
  requestId?: string;
  lifetimeSeconds?: number;
}

export function buildSigningBytes(input: SigningInput): Buffer {
  const value = [
    input.proofVersion,
    `caller:${input.callerB64Url}`,
    `key-id:${input.keyIdB64Url}`,
    `audience:${input.audience}`,
    `method:${input.method}`,
    `target:${input.targetB64Url}`,
    `body-sha256:${input.bodySha256}`,
    `issued-at:${input.issuedAt}`,
    `expires-at:${input.expiresAt}`,
    `request-id:${input.requestId}`,
    `provider:${input.providerB64Url}`,
    `provider-subject:${input.providerSubjectB64Url}`,
  ].join("\n");
  return Buffer.from(value, "ascii");
}

export function signRequest(key: Buffer, input: SigningInput): string {
  return createHmac("sha256", key)
    .update(buildSigningBytes(input))
    .digest()
    .toString("base64url");
}

export function decodeSecretValue(value: string): Buffer {
  const withoutLf = value.endsWith("\n") ? value.slice(0, -1) : value;
  if (
    withoutLf.length === 0 ||
    !/^[A-Za-z0-9_-]+$/.test(withoutLf) ||
    withoutLf.includes("=")
  ) {
    throw new Error("secret must be one unpadded base64url value");
  }
  const decoded = Buffer.from(withoutLf, "base64url");
  if (decoded.length !== 32 || decoded.toString("base64url") !== withoutLf) {
    throw new Error("secret must decode to exactly 32 bytes");
  }
  return decoded;
}

export function loadSecretFile(path: string): Buffer {
  return decodeSecretValue(readFileSync(path, "utf8"));
}

export function encodeText(value: string): string {
  return Buffer.from(value, "utf8").toString("base64url");
}

export function createProofHeaders(options: ProofRequestOptions): {
  headers: ProofHeaders;
  requestId: string;
  body: Buffer;
} {
  const body = options.body ?? Buffer.alloc(0);
  const issuedAt = Math.floor(options.now ?? Date.now() / 1000);
  const lifetimeSeconds = options.lifetimeSeconds ?? 10;
  if (
    !Number.isSafeInteger(issuedAt) ||
    issuedAt < 0 ||
    !Number.isSafeInteger(lifetimeSeconds)
  ) {
    throw new Error("proof timestamps must be safe integers");
  }
  if (lifetimeSeconds < 1 || lifetimeSeconds > 30) {
    throw new Error("proof lifetime must be between one and thirty seconds");
  }
  if (issuedAt > Number.MAX_SAFE_INTEGER - lifetimeSeconds) {
    throw new Error("proof timestamps must be safe integers");
  }
  if (options.key.length !== 32) {
    throw new Error("proof key must be exactly 32 bytes");
  }
  if (!/^[A-Z]+$/.test(options.method)) {
    throw new Error("proof method must contain uppercase ASCII letters only");
  }
  if (
    options.rawTarget.length === 0 ||
    [...options.rawTarget].some((character) => {
      const code = character.charCodeAt(0);
      return code < 0x20 || code > 0x7e;
    })
  ) {
    throw new Error("proof target must be ASCII");
  }
  const provider = options.provider ?? "";
  const providerSubject = options.providerSubject ?? "";
  if (options.caller.length === 0) {
    throw new Error("proof caller must not be empty");
  }
  if (options.keyId.length === 0) {
    throw new Error("proof key ID must not be empty");
  }
  if (Boolean(provider) !== Boolean(providerSubject)) {
    throw new Error("provider and provider subject must be both empty or non-empty");
  }
  const requestId = options.requestId ?? randomUUID();
  if (!isCanonicalUuid(requestId)) {
    throw new Error("proof request ID must be a canonical lowercase UUID");
  }
  const rawTarget = Buffer.from(options.rawTarget, "ascii");
  const input: SigningInput = {
    proofVersion: PROOF_VERSION,
    callerB64Url: encodeText(options.caller),
    keyIdB64Url: encodeText(options.keyId),
    audience: AUDIENCE,
    method: options.method,
    targetB64Url: rawTarget.toString("base64url"),
    bodySha256: createHash("sha256").update(body).digest("hex"),
    issuedAt: String(issuedAt),
    expiresAt: String(issuedAt + lifetimeSeconds),
    requestId,
    providerB64Url: encodeText(provider),
    providerSubjectB64Url: encodeText(providerSubject),
  };
  return {
    requestId,
    body,
    headers: {
      "X-ClashLens-Proof-Version": input.proofVersion,
      "X-ClashLens-Caller": input.callerB64Url,
      "X-ClashLens-Key-Id": input.keyIdB64Url,
      "X-ClashLens-Issued-At": input.issuedAt,
      "X-ClashLens-Expires-At": input.expiresAt,
      "X-ClashLens-Request-Id": input.requestId,
      "X-ClashLens-Provider": input.providerB64Url,
      "X-ClashLens-Provider-Subject": input.providerSubjectB64Url,
      "X-ClashLens-Signature": signRequest(options.key, input),
    },
  };
}
