import { useEffect, useRef } from "react";
import { useLoaderData } from "react-router";

import { isOverlayLinkId } from "../lib/obs-overlay";
import type { BareHandle } from "../root";
import type { Route } from "./+types/obs-overlay";
import "../obs-overlay.css";

export const handle: BareHandle = { bare: true };

export function meta() {
  return [{ title: "Clash Lens overlay" }, { name: "robots", content: "noindex" }];
}

export function loader({ params }: Route.LoaderArgs) {
  if (params.link === undefined) return { link: null };
  return { link: isOverlayLinkId(params.link) ? params.link : "" };
}

export function headers() {
  return { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" };
}

export default function ObsOverlayRoute() {
  const { link } = useLoaderData<typeof loader>();
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!root.current || link === "") return;
    return mountOverlayOnce(root.current, link);
  }, [link]);
  return <div ref={root} className="obs-overlay" />;
}

function mountOverlayOnce(element: HTMLElement, link: string | null): () => void {
  let stop: (() => void) | null = null;
  let cancelled = false;
  void import("../lib/obs-overlay-view").then(({ mountOverlay }) => {
    if (!cancelled) stop = mountOverlay(element, { link, search: location.search });
  });
  return () => {
    cancelled = true;
    stop?.();
  };
}
