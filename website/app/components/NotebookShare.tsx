import { useEffect, useRef, useState } from "react";

import type { BaseShare } from "../lib/notebook";
import { sharePath, type ShareChange } from "../lib/notebook-share";
import { Sheet } from "./NotebookParts";

const SHARE = "/notebook/share";

const ERRORS: Record<string, string> = {
  base_not_found: "That base was deleted. Refresh the page.",
  share_not_found: "That base isn't shared any more. Tap Share to make a new link.",
};
const UNAVAILABLE = "Couldn't reach Clash Lens just now. Try again in a moment.";

/** One change through /notebook/share: how the base is shared after it. */
async function send(change: ShareChange): Promise<BaseShare | null> {
  const response = await fetch(SHARE, {
    method: "POST",
    body: new URLSearchParams({ change: JSON.stringify(change) }),
  });
  const answer = (await response.json().catch(() => ({}))) as {
    share?: BaseShare | null;
    error?: string;
  };
  if (!response.ok || answer.share === undefined)
    throw new Error(ERRORS[answer.error ?? ""] ?? UNAVAILABLE, { cause: answer.error });
  return answer.share;
}

/** Copies the address `text` resolves to. The copy starts inside the tap,
 * as phone browsers ask, even when the link is still being made. */
async function copyLink(text: Promise<string>): Promise<boolean> {
  try {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/plain": text.then((value) => new Blob([value], { type: "text/plain" })),
      }),
    ]);
    return true;
  } catch {
    try {
      await navigator.clipboard.writeText(await text);
      return true;
    } catch {
      return false;
    }
  }
}

/** The base page's Share button: one tap makes the unlisted link, copies it
 * and opens the share sheet, where Stop sharing is one more tap. */
export function ShareButton({
  base,
  name,
  share,
}: {
  base: string;
  name: string;
  share: BaseShare | null;
}) {
  const [current, setCurrent] = useState(share);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function start() {
    setOpen(true);
    setError(null);
    setCopied(null);
    const made = send({ action: "share", base });
    setBusy(true);
    const address = made.then((shared) => {
      if (!shared) throw new Error(UNAVAILABLE);
      return location.origin + sharePath(shared.link);
    });
    void copyLink(address).then(setCopied);
    made
      .then(setCurrent)
      .catch((cause: Error) => setError(cause.message))
      .finally(() => setBusy(false));
  }

  function change(next: ShareChange) {
    const before = current;
    // A switch moves at once, and back if the save fails.
    if (current && next.action === "settings")
      setCurrent({
        ...current,
        includeLink: next.include_link ?? current.includeLink,
        showNames: next.show_names ?? current.showNames,
      });
    setBusy(true);
    setError(null);
    send(next)
      .then((shared) => {
        setCurrent(shared);
        if (!shared) setCopied(null);
      })
      .catch((failure: Error) => {
        setCurrent(failure.cause === "share_not_found" ? null : before);
        setError(failure.message);
      })
      .finally(() => setBusy(false));
  }

  return (
    <>
      <button type="button" className="button" onClick={start}>
        Share
      </button>
      {open ? (
        <ShareSheet
          name={name}
          share={current}
          copied={copied}
          busy={busy}
          error={error}
          onCopied={setCopied}
          onChange={(settings) => change({ action: "settings", base, ...settings })}
          onStop={() => change({ action: "stop", base })}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

function ShareSheet({
  name,
  share,
  copied,
  busy,
  error,
  onCopied,
  onChange,
  onStop,
  onClose,
}: {
  name: string;
  share: BaseShare | null;
  copied: boolean | null;
  busy: boolean;
  error: string | null;
  onCopied: (copied: boolean) => void;
  onChange: (settings: { include_link?: boolean; show_names?: boolean }) => void;
  onStop: () => void;
  onClose: () => void;
}) {
  const [stopped, setStopped] = useState(false);
  const [canSend, setCanSend] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => setCanSend(typeof navigator.share === "function"), []);
  const address = share ? location.origin + sharePath(share.link) : "";
  return (
    <Sheet title={`Share ${name}`} onClose={onClose}>
      {error ? (
        <p className="nb-share-error" role="alert">
          {error}
        </p>
      ) : null}
      {share ? (
        <>
          <p className="nb-share-state" aria-live="polite">
            {copied ? "Link copied." : copied === false ? "Copy the link below." : " "}
          </p>
          <div className="nb-share-link">
            <input
              ref={field}
              readOnly
              value={address}
              aria-label="Link to this base"
              onFocus={(event) => event.target.select()}
            />
            <button
              type="button"
              className="button"
              onClick={() => {
                void copyLink(Promise.resolve(address)).then((done) => {
                  if (!done) field.current?.select();
                  onCopied(done);
                });
              }}
            >
              Copy
            </button>
          </div>
          {canSend ? (
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                navigator.share({ url: address }).catch(() => undefined);
              }}
            >
              Send…
            </button>
          ) : null}
          <p className="nb-hint">
            Anyone with the link can see this base and its record. Search engines don't
            list it.
          </p>
          <fieldset className="nb-share-switches" disabled={busy}>
            <legend className="sr-only">What the link shows</legend>
            <label className="nb-switch">
              <input
                type="checkbox"
                checked={share.includeLink}
                onChange={(event) => onChange({ include_link: event.target.checked })}
              />
              <span>
                Include base link
                <small>Lets them open the layout in the game.</small>
              </span>
            </label>
            <label className="nb-switch">
              <input
                type="checkbox"
                checked={share.showNames}
                onChange={(event) => onChange({ show_names: event.target.checked })}
              />
              <span>
                Show my player names
                <small>With each account's record on it.</small>
              </span>
            </label>
          </fieldset>
          {/* Sharing to a Crew goes here, beside the link. */}
          <div className="nb-delete">
            <button
              type="button"
              className="button danger-button"
              disabled={busy}
              onClick={() => {
                setStopped(true);
                onStop();
              }}
            >
              Stop sharing
            </button>
            <p className="nb-hint">The link stops working for good.</p>
          </div>
        </>
      ) : stopped && !busy && !error ? (
        <p className="nb-share-state" role="status">
          Sharing stopped. The old link shows nothing now. Share again to make a new one.
        </p>
      ) : busy ? (
        <p className="nb-share-state">Making the link…</p>
      ) : null}
    </Sheet>
  );
}
