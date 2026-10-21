import {
  parseNotebook,
  parsePicture,
  parseSaved,
  type BasePicture,
  type Notebook,
  type NotebookChange,
  type NotebookSaved,
} from "../lib/notebook";
import { PythonApiError, requestJson, type GoogleAccountIdentity } from "./python.server";

/** The account's notebook this Season; with `base`, also that base's note,
 * link and record on each player. */
export async function getNotebook(
  identity: GoogleAccountIdentity,
  base?: string,
): Promise<Notebook> {
  const query = base === undefined ? "" : `?base=${encodeURIComponent(base)}`;
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

/** Make `image` the base's picture, or with null remove it; its new links. */
export async function saveBasePicture(
  identity: GoogleAccountIdentity,
  base: string,
  image: Buffer | null,
): Promise<BasePicture | null> {
  const payload = await requestJson<{ picture?: unknown }>(
    "/v1/account/notebook/picture",
    "POST",
    Buffer.from(
      JSON.stringify({ base, image: image === null ? null : image.toString("base64") }),
      "utf8",
    ),
    "notebook-picture",
    undefined,
    identity,
    undefined,
    // A large photo takes a second or two to shrink and store.
    30_000,
  );
  const picture = parsePicture(payload.picture);
  if (picture === undefined) throw new PythonApiError(502, { error: "malformed" });
  return picture;
}
