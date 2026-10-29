import { Outlet, useMatches } from "react-router";

import { SaveNotice, useNotebookSave } from "../components/NotebookParts";
import type { Notebook } from "../lib/notebook";

/** The Notebook and base pages share one save and its toast, so a deleted
 * base's Undo is still there on the Notebook it goes back to. */
export default function NotebookLayout() {
  const notebook =
    useMatches()
      .map(
        (match) =>
          (match.loaderData as { notebook?: Notebook | null } | undefined)?.notebook,
      )
      .find(Boolean) ?? null;
  const saver = useNotebookSave(notebook);
  return (
    <>
      <Outlet context={saver} />
      <SaveNotice saver={saver} />
    </>
  );
}
