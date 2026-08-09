import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const root = new URL("../build/client/", import.meta.url);
const forbidden = [
  "python.server",
  "python-client.server",
  "hmac.server",
  "signer.server",
  "fixture_server",
  "google-oidc.server",
  "auth-cookies.server",
  "config.server",
  "openid-client",
  "providerSubject",
  "X-ClashLens-Provider-Subject",
  "CLASHLENS_PUBLIC_ORIGIN",
  "CLASHLENS_GOOGLE_CLIENT",
  "CLASHLENS_LOGIN_",
  "CLASHLENS_PYTHON_API_URL",
  "CLASHLENS_PYTHON_HMAC_CALLER",
  "CLASHLENS_PYTHON_HMAC_KEY_ID",
  "CLASHLENS_PYTHON_HMAC_SECRET",
  "CLASHLENS_PYTHON_HMAC_SECRET_FILE",
  "CLASHLENS_FIXTURE_HMAC_SECRET",
  "CLASHLENS_FIXTURE_HMAC_CALLER",
  "CLASHLENS_FIXTURE_HMAC_KEY_ID",
  "api.clashofclans.com",
  "clashlens-private",
  "POSTGRES",
];

async function files(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const result = [];
  for (const entry of entries) {
    const path = join(directory.pathname, entry.name);
    if (entry.isDirectory())
      result.push(...(await files(new URL(`${entry.name}/`, directory))));
    else result.push(path);
  }
  return result;
}

const assetFiles = await files(root);
const findings = [];
for (const file of assetFiles) {
  const source = await readFile(file, "utf8");
  for (const marker of forbidden) {
    if (source.includes(marker)) findings.push(`${file}: ${marker}`);
  }
}
if (findings.length > 0) {
  console.error("Forbidden server-only markers found in browser assets:");
  console.error(findings.join("\n"));
  process.exit(1);
}
console.log(`Browser asset boundary check passed (${assetFiles.length} files).`);
