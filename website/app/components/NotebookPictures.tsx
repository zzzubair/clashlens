import { useEffect, useRef, useState } from "react";
import { useRevalidator } from "react-router";

import { MAX_PICTURE_BYTES, type BasePicture, type NotebookBase } from "../lib/notebook";

const PICTURE = "/notebook/picture";
// The kinds the private API accepts. Asking for these, an iPhone turns its
// own HEIC photos into JPEG.
const PICTURE_TYPES = "image/jpeg,image/png,image/webp";

const ERRORS: Record<string, string> = {
  not_a_picture:
    "That file isn't a picture. Use a JPEG, PNG or WebP screenshot or photo.",
  picture_too_large: "That picture is too big. Pictures can be up to 15 MB.",
  daily_limit: "You've added 30 pictures in the last 24 hours. Try again later.",
  base_not_found: "That base was deleted. Refresh the page.",
  pictures_unavailable: "Pictures can't be saved just now. Try again later.",
  unavailable: "Couldn't save the picture just now. Try again in a moment.",
};

/** Upload or remove one base's picture, then read the page again. */
export function usePictureSave(base: string) {
  const revalidator = useRevalidator();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function send(file: File | null): Promise<void> {
    if (file && file.size > MAX_PICTURE_BYTES) {
      setError(ERRORS.picture_too_large!);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${PICTURE}?base=${base}${file ? "" : "&remove=1"}`, {
        method: "POST",
        // A file the phone gives no kind still goes, for the server to read.
        headers: file
          ? {
              "Content-Type": file.type.startsWith("image/")
                ? file.type
                : "image/unknown",
            }
          : {},
        body: file,
      });
      const answer = (await response.json().catch(() => ({}))) as { error?: string };
      if (!response.ok) {
        setError(ERRORS[answer.error ?? ""] ?? ERRORS.unavailable!);
        return;
      }
      await revalidator.revalidate();
    } catch {
      setError(ERRORS.unavailable!);
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, send };
}

/** A button-looking label that opens the phone's photo picker. */
export function PictureInput({
  label,
  className,
  disabled,
  onFile,
}: {
  label: string;
  className: string;
  disabled?: boolean;
  onFile: (file: File) => void;
}) {
  return (
    <label className={`${className}${disabled ? " nb-disabled" : ""}`}>
      <input
        type="file"
        accept={PICTURE_TYPES}
        className="sr-only"
        disabled={disabled}
        onChange={(event) => {
          const file = event.target.files?.[0];
          // The same file picked again still counts as a change.
          event.target.value = "";
          if (file) onFile(file);
        }}
      />
      {label}
    </label>
  );
}

/** A base's picture full size over the page. It closes on its ✕, a tap
 * outside the picture, Escape or Back, and the address never changes. */
export function PicturePreview({
  name,
  picture,
  onClose,
}: {
  name: string;
  picture: BasePicture;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (!element.open) element.showModal();
    // Back needs a history entry to take. This one copies the page's own,
    // address and all, and the router never hears of it: these listeners
    // run before the router's and stop it.
    window.history.pushState(window.history.state, "");
    let popped = false;
    const onPop = (event: PopStateEvent) => {
      popped = true;
      event.stopImmediatePropagation();
      element.close();
    };
    const closed = () => close.current();
    window.addEventListener("popstate", onPop, { capture: true });
    element.addEventListener("close", closed);
    return () => {
      element.removeEventListener("close", closed);
      window.removeEventListener("popstate", onPop, { capture: true });
      if (popped) return;
      // Closed some other way: take the entry back, unheard too.
      window.addEventListener("popstate", (event) => event.stopImmediatePropagation(), {
        capture: true,
        once: true,
      });
      window.history.back();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="nb-preview"
      aria-label={`${name}, picture`}
      onClick={(event) => {
        if (event.target === event.currentTarget) dialog.current?.close();
      }}
    >
      <figure>
        <img src={picture.full} alt={name} />
        <figcaption>{name}</figcaption>
      </figure>
      <button
        type="button"
        className="nb-close nb-preview-close"
        aria-label="Close"
        onClick={() => dialog.current?.close()}
      >
        ✕
      </button>
    </dialog>
  );
}

/** A button on a base's picture that opens it full size: `cover` lies over
 * a tile drawn underneath, as in a list row that is itself a link. */
export function PictureButton({
  base,
  picture,
  size = "sm",
}: {
  base: NotebookBase;
  picture: BasePicture;
  size?: "sm" | "lg" | "cover";
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className={
          size === "cover" ? "nb-tile-cover" : `nb-tile nb-tile-${size} nb-tile-picture`
        }
        aria-label={`See ${base.name}'s picture`}
        onClick={() => setOpen(true)}
      >
        {size === "cover" ? null : (
          <img
            src={size === "lg" ? picture.full : picture.small}
            alt=""
            loading="lazy"
            decoding="async"
          />
        )}
      </button>
      {open ? (
        <PicturePreview
          name={base.name}
          picture={picture}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

/** The top of a base's page: its picture, which opens full size, or a
 * place to add one. */
export function BasePictureArea({ base }: { base: NotebookBase }) {
  const { busy, error, send } = usePictureSave(base.id);
  return (
    <>
      {base.picture ? (
        <PictureButton base={base} picture={base.picture} size="lg" />
      ) : (
        <PictureInput
          className="nb-tile nb-tile-lg nb-tile-add-picture"
          label={busy ? "Adding the picture…" : "+ Add a picture"}
          disabled={busy}
          onFile={(file) => void send(file)}
        />
      )}
      {error ? (
        <p className="nb-picture-error" role="alert">
          {error}
        </p>
      ) : null}
    </>
  );
}

/** The Edit sheet's picture: change or remove it, or add one. */
export function PictureField({ base }: { base: NotebookBase }) {
  const { busy, error, send } = usePictureSave(base.id);
  return (
    <div className="nb-picture-field">
      <span className="nb-field-label">Picture</span>
      <div className="nb-picture-actions">
        <PictureInput
          className="button secondary"
          label={busy ? "Saving…" : base.picture ? "Change picture" : "Add a picture"}
          disabled={busy}
          onFile={(file) => void send(file)}
        />
        {base.picture ? (
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={() => void send(null)}
          >
            Remove picture
          </button>
        ) : null}
      </div>
      {error ? (
        <p className="nb-picture-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
