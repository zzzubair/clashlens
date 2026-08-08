import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  buildSigningBytes,
  createProofHeaders,
  decodeSecretValue,
  signRequest,
  type SigningInput,
} from "../../app/server/signer.server";

const vectorsPath = fileURLToPath(
  new URL("../../../testdata/private-api-hmac-v1.json", import.meta.url),
);
const vectors = JSON.parse(readFileSync(vectorsPath, "utf8")) as {
  vectors: Array<{
    key_hex: string;
    proof_version: string;
    caller: string;
    caller_b64url: string;
    key_id: string;
    key_id_b64url: string;
    audience: string;
    method: string;
    target: string;
    target_b64url: string;
    body_hex: string;
    body_sha256: string;
    issued_at: string;
    expires_at: string;
    verification_time: number;
    request_id: string;
    provider: string;
    provider_b64url: string;
    provider_subject: string;
    provider_subject_b64url: string;
    signing_bytes_hex: string;
    signature_b64url: string;
  }>;
};

function inputFromVector(vector: (typeof vectors.vectors)[number]): SigningInput {
  return {
    proofVersion: vector.proof_version,
    callerB64Url: vector.caller_b64url,
    keyIdB64Url: vector.key_id_b64url,
    audience: vector.audience,
    method: vector.method,
    targetB64Url: vector.target_b64url,
    bodySha256: vector.body_sha256,
    issuedAt: vector.issued_at,
    expiresAt: vector.expires_at,
    requestId: vector.request_id,
    providerB64Url: vector.provider_b64url,
    providerSubjectB64Url: vector.provider_subject_b64url,
  };
}

describe("private API HMAC v1 signer", () => {
  it("matches every language-neutral signing byte and signature vector", () => {
    for (const vector of vectors.vectors) {
      const input = inputFromVector(vector);
      const key = Buffer.from(vector.key_hex, "hex");

      expect(Buffer.from(buildSigningBytes(input)).toString("hex")).toBe(
        vector.signing_bytes_hex,
      );
      expect(signRequest(key, input)).toBe(vector.signature_b64url);
    }
  });

  it("builds the complete anonymous proof from one golden vector", () => {
    const vector = vectors.vectors[0];
    const proof = createProofHeaders({
      key: Buffer.from(vector.key_hex, "hex"),
      caller: vector.caller,
      keyId: vector.key_id,
      method: vector.method,
      rawTarget: vector.target,
      now: vector.verification_time,
      requestId: vector.request_id,
      lifetimeSeconds: 30,
    });

    expect(proof.body).toEqual(Buffer.alloc(0));
    expect(proof.headers["X-ClashLens-Proof-Version"]).toBe(vector.proof_version);
    expect(proof.headers["X-ClashLens-Caller"]).toBe(vector.caller_b64url);
    expect(proof.headers["X-ClashLens-Key-Id"]).toBe(vector.key_id_b64url);
    expect(proof.headers["X-ClashLens-Issued-At"]).toBe(vector.issued_at);
    expect(proof.headers["X-ClashLens-Expires-At"]).toBe(vector.expires_at);
    expect(proof.headers["X-ClashLens-Request-Id"]).toBe(vector.request_id);
    expect(proof.headers["X-ClashLens-Provider"]).toBe("");
    expect(proof.headers["X-ClashLens-Provider-Subject"]).toBe("");
    expect(proof.headers["X-ClashLens-Signature"]).toBe(vector.signature_b64url);
  });

  it("rejects non-canonical request IDs and invalid proof fields", () => {
    const options = {
      key: Buffer.alloc(32),
      caller: "typescript-website",
      keyId: "2026-08-a",
      method: "GET",
      rawTarget: "/v1/players/%232PP",
      now: 1_800_000_000,
      lifetimeSeconds: 10,
    } as const;

    expect(() => createProofHeaders({ ...options, requestId: "not-a-uuid" })).toThrow(
      "request ID",
    );
    expect(() => createProofHeaders({ ...options, method: "get" })).toThrow("method");
    expect(() => createProofHeaders({ ...options, rawTarget: "/bad\npath" })).toThrow(
      "target",
    );
    expect(() => createProofHeaders({ ...options, rawTarget: "" })).toThrow("target");
    expect(() => createProofHeaders({ ...options, caller: "" })).toThrow("caller");
    expect(() => createProofHeaders({ ...options, key: Buffer.alloc(31) })).toThrow(
      "key",
    );
    expect(() => createProofHeaders({ ...options, now: -1 })).toThrow("timestamps");
  });

  it("accepts one final LF and rejects other secret-file encodings", () => {
    const encoded = Buffer.alloc(32).toString("base64url");
    expect(decodeSecretValue(`${encoded}\n`)).toEqual(Buffer.alloc(32));
    expect(() => decodeSecretValue(`${encoded}\r\n`)).toThrow("secret");
    expect(() => decodeSecretValue(`${encoded}=`)).toThrow("secret");
  });
});
