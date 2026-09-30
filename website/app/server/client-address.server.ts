import { isIP } from "node:net";
import { createContext, RouterContextProvider } from "react-router";

export const clientAddressContext = createContext<string | undefined>(undefined);

function normalizeAddress(value: string): string | undefined {
  if (isIP(value) === 4) return value;
  if (isIP(value) !== 6 || value.includes("%")) return undefined;
  const address = new URL(`http://[${value}]/`).hostname.slice(1, -1);
  const mapped = /^::ffff:([a-f0-9]+):([a-f0-9]+)$/.exec(address);
  if (!mapped) return address;
  const high = parseInt(mapped[1], 16);
  const low = parseInt(mapped[2], 16);
  return `${high >> 8}.${high & 255}.${low >> 8}.${low & 255}`;
}

export function createClientAddressContext(
  env: Record<string, string | undefined> = process.env,
) {
  const rawProxy = env.CLASHLENS_TRUSTED_PROXY_IP;
  const proxy = rawProxy ? normalizeAddress(rawProxy) : undefined;
  const header = env.CLASHLENS_CLIENT_IP_HEADER ?? "CF-Connecting-IP";
  if ((rawProxy && !proxy) || !/^[!#$%&'*+.^_`|~\w-]+$/.test(header)) {
    throw new Error(
      "Invalid trusted proxy address or client address header configuration",
    );
  }
  return (request: Request, client: { address: string }) => {
    const peer = normalizeAddress(client.address);
    const forwarded = proxy && peer === proxy ? request.headers.get(header) : null;
    // Accept one literal address, never a forwarding chain, port, or hostname.
    const address = (forwarded && normalizeAddress(forwarded)) || peer;
    const context = new RouterContextProvider();
    context.set(clientAddressContext, address);
    return context;
  };
}
