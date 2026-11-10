import { expect } from "@playwright/test";
import { UNSAFE_decodeViaTurboStream } from "react-router";
import type { PlayerPage } from "../../../app/lib/contracts";

// React Router page data is a flat list of values: objects map "_<key index>"
// to value indexes, arrays list value indexes, -5 is null and -7 undefined.
// A streamed value is ["P", its own index], resolved by a later "P<index>:"
// line; it decodes to undefined, so past Seasons are left out.
export function decodePageData(text: string) {
  const values: unknown[] = JSON.parse(text.split("\n")[0]);
  const decode = (index: number): unknown => {
    if (index < 0) return index === -5 ? null : undefined;
    const value = values[index];
    if (Array.isArray(value)) return value[0] === "P" ? undefined : value.map(decode);
    if (value === null || typeof value !== "object") return value;
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        values[Number(key.slice(1))],
        decode(item as number),
      ]),
    );
  };
  return decode(0) as Record<string, { data?: { player?: PlayerPage } }>;
}
export function encodePageData(data: unknown) {
  const values: unknown[] = [];
  const encode = (value: unknown): number => {
    if (value === null) return -5;
    if (value === undefined) return -7;
    if (typeof value !== "object") return values.push(value) - 1;
    const index = values.push(null) - 1;
    values[index] = Array.isArray(value)
      ? value.map(encode)
      : Object.fromEntries(
          Object.entries(value).map(([key, item]) => [`_${encode(key)}`, encode(item)]),
        );
    return index;
  };
  encode(data);
  return `${JSON.stringify(values)}\n`;
}
export function pagePlayer(data: ReturnType<typeof decodePageData>) {
  const player = Object.values(data).find((route) => route.data?.player)?.data?.player;
  expect(player?.currentDay, "saved player has a current Legend day").toBeTruthy();
  return player!;
}
export async function withServerData(
  html: string,
  edit: (saved: { player: PlayerPage; seasons?: unknown }) => void,
) {
  const enqueue =
    /window\.__reactRouterContext\.streamController\.enqueue\(("(?:[^"\\]|\\.)*")\);/g;
  const stream = [...html.matchAll(enqueue)]
    .map((match) => JSON.parse(match[1]) as string)
    .join("");
  const decoded = await UNSAFE_decodeViaTurboStream(
    new Response(stream).body!,
    globalThis,
  );
  await decoded.done;
  const data = decoded.value as {
    loaderData: Record<string, { player: PlayerPage; pastSeasons: unknown }>;
  };
  const saved = data.loaderData["routes/player"];
  edit(saved);
  for (const [key, value] of Object.entries(saved))
    Object.assign(saved, { [key]: await value });
  const escaped = JSON.stringify(encodePageData(data)).replace(
    /[&<>\u2028\u2029]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  let first = true;
  const served = html.replace(enqueue, () => {
    if (!first) return "";
    first = false;
    return `window.__reactRouterContext.streamController.enqueue(${escaped});`;
  });
  expect(served).not.toBe(html);
  return served;
}
export function withServerAge(html: string, ageSeconds: number) {
  return withServerData(html, (saved) => {
    saved.player.profile.freshness.ageSeconds = ageSeconds;
  });
}
