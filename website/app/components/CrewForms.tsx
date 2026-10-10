import { useEffect, useRef, useState } from "react";
import { Form, useFetcher, useSearchParams } from "react-router";

import { formatInviteExpiry, type InviteLink } from "../lib/crew-contracts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { ErrorNotice } from "./ErrorNotice";

interface InviteAnswer {
  idempotencyKey: string;
  error: string | WebsiteErrorResponse | null;
  invite: InviteLink | null;
}

/**
 * Invite on a crew page: makes or reuses the clasher's link and shows it in
 * a sheet with Copy and Share. Without JavaScript the form posts, the page
 * comes back with the sheet open, and the link is plain selectable text.
 * A crew just made (`?invite=1`) opens the sheet straight away.
 */
export function CrewInvite({
  crewName,
  idempotencyKey,
  answer,
}: {
  crewName: string;
  idempotencyKey: string;
  /** The answer when the form posted without JavaScript. */
  answer: InviteAnswer | null;
}) {
  const fetcher = useFetcher<InviteAnswer>();
  const [searchParams] = useSearchParams();
  const [dismissed, setDismissed] = useState(false);
  const shown = fetcher.data ?? answer;
  const key = shown?.idempotencyKey ?? idempotencyKey;
  const busy = fetcher.state === "submitting";
  const asked = useRef(false);

  useEffect(() => {
    if (asked.current || searchParams.get("invite") !== "1") return;
    asked.current = true;
    void fetcher.submit({ intent: "invite", idempotencyKey: key }, { method: "post" });
  }, [fetcher, key, searchParams]);

  const open = shown !== null && shown !== undefined && !dismissed;
  return (
    <>
      <fetcher.Form method="post" onSubmit={() => setDismissed(false)}>
        <input type="hidden" name="intent" value="invite" />
        <input type="hidden" name="idempotencyKey" value={key} />
        <button type="submit" className="button button-primary" disabled={busy}>
          Invite
        </button>
      </fetcher.Form>
      {open ? (
        <Sheet label={`Invite to ${crewName}`} onClose={() => setDismissed(true)}>
          {shown.invite ? (
            <InviteBody invite={shown.invite} />
          ) : (
            <FormResult result={{ notice: null, error: shown.error }} />
          )}
          {shown.invite ? (
            <fetcher.Form method="post" className="crew-new-link">
              <input type="hidden" name="intent" value="invite" />
              <input type="hidden" name="new" value="1" />
              <input type="hidden" name="idempotencyKey" value={key} />
              <button type="submit" className="crew-link-button">
                Make a new link
              </button>
            </fetcher.Form>
          ) : null}
        </Sheet>
      ) : null}
    </>
  );
}

function InviteBody({ invite }: { invite: InviteLink }) {
  const [copied, setCopied] = useState(false);
  const [canShare, setCanShare] = useState(false);
  useEffect(() => setCanShare(typeof navigator.share === "function"), []);
  useEffect(() => setCopied(false), [invite.link]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(invite.link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <>
      <div className="crew-linkbox">
        <code>{invite.link}</code>
        <button type="button" className="button button-primary" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="crew-sheet-note">
        Works until {formatInviteExpiry(invite.expiresAt)} · {invite.openPlaces}{" "}
        {invite.openPlaces === 1 ? "place" : "places"} open
      </p>
      {canShare ? (
        <button
          type="button"
          className="button button-primary crew-share"
          onClick={() => {
            navigator.share({ url: invite.link }).catch(() => undefined);
          }}
        >
          Share link
        </button>
      ) : null}
    </>
  );
}

/**
 * A sheet over the page. It is a modal dialog once the page's JavaScript
 * runs; before that it shows in place, so it works without JavaScript.
 */
export function Sheet({
  label,
  onClose,
  children,
}: {
  label: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  // Closing the in-place sheet to reopen it as a modal fires a close event
  // too; that one isn't the clasher closing it.
  const reopening = useRef(false);
  useEffect(() => {
    const element = dialog.current;
    if (element === null || typeof element.showModal !== "function") return;
    if (element.open) {
      reopening.current = true;
      element.close();
    }
    element.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      open
      className="crew-sheet"
      aria-labelledby="crew-sheet-title"
      onClose={() => {
        if (reopening.current) reopening.current = false;
        else onClose();
      }}
      onClick={(event) => {
        // A tap on the backdrop, outside the sheet's own box, closes it.
        if (event.target === event.currentTarget) event.currentTarget.close();
      }}
    >
      <div className="crew-sheet-body">
        <div className="crew-sheet-head">
          <h2 id="crew-sheet-title">{label}</h2>
          <form method="dialog">
            <button type="submit" className="crew-close" aria-label="Close">
              ✕
            </button>
          </form>
        </div>
        {children}
      </div>
    </dialog>
  );
}

/** What the last form on a crew page did, or why it was refused. */
export function FormResult({
  result,
}: {
  result:
    { notice: string | null; error: string | WebsiteErrorResponse | null } | undefined;
}) {
  if (result?.notice) {
    return (
      <p className="notice crew-done" role="status">
        {result.notice}
      </p>
    );
  }
  if (!result?.error) return null;
  return typeof result.error === "string" ? (
    <p className="notice" role="alert">
      {result.error}
    </p>
  ) : (
    <ErrorNotice error={result.error} />
  );
}

/**
 * A button that asks before it acts: it opens a short question with the
 * real button under it. A disclosure, so it works without JavaScript.
 */
export function ConfirmForm({
  label,
  question,
  confirm,
  intent,
  idempotencyKey,
  fields = {},
  danger = true,
}: {
  label: React.ReactNode;
  question: React.ReactNode;
  confirm: string;
  intent: string;
  idempotencyKey: string;
  fields?: Record<string, string>;
  danger?: boolean;
}) {
  return (
    <details className="crew-confirm">
      <summary className="button secondary">{label}</summary>
      <Form method="post" className="crew-confirm-body">
        <p>{question}</p>
        <input type="hidden" name="intent" value={intent} />
        <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
        {Object.entries(fields).map(([name, value]) => (
          <input key={name} type="hidden" name={name} value={value} />
        ))}
        <button
          type="submit"
          className={danger ? "button danger-button" : "button button-primary"}
        >
          {confirm}
        </button>
      </Form>
    </details>
  );
}
