import {
  parseNotebook,
  parseSaved,
  type Notebook,
  type NotebookChange,
  type NotebookSaved,
} from "../lib/notebook";
import { PythonApiError, requestJson, type GoogleAccountIdentity } from "./python.server";

/** The account's notebook as this Season's page, or `season`'s; with
 * `base`, also that base's note, link and record on each of the account's
 * players. */
export async function getNotebook(
  identity: GoogleAccountIdentity,
  { season, base }: { season?: string; base?: string } = {},
): Promise<Notebook> {
  const params = new URLSearchParams();
  if (season !== undefined) params.set("season", season);
  if (base !== undefined) params.set("base", base);
  const query = params.size ? `?${params.toString()}` : "";
  const payload = await requestJson<unknown>(
    `/v1/account/notebook${query}`,
    "GET",
    undefined,
    "notebook",
    undefined,
    identity,
  );
  const notebook = parseNotebook(payload);
  if (notebook === null) throw new PythonApiError(502, { error: "malformed" });
  return notebook;
}

/** Apply one change; says what it did and what undoes it. */
export async function saveNotebook(
  identity: GoogleAccountIdentity,
  change: NotebookChange,
): Promise<NotebookSaved> {
  const payload = await requestJson<unknown>(
    "/v1/account/notebook",
    "POST",
    Buffer.from(JSON.stringify(change), "utf8"),
    "notebook-saved",
    undefined,
    identity,
  );
  const saved = parseSaved(payload);
  if (saved === null) throw new PythonApiError(502, { error: "malformed" });
  return saved;
}
