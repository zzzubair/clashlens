import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticRouter,
  StaticRouterProvider,
  type createStaticHandler,
} from "react-router";

export function renderStaticRoute(
  handler: ReturnType<typeof createStaticHandler>,
  context: Awaited<ReturnType<ReturnType<typeof createStaticHandler>["query"]>>,
) {
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

export function unwrap<T>(result: unknown): { data: T; status: number } {
  const wrapped = result as { data: T; init: { status?: number } | null };
  return { data: wrapped.data, status: wrapped.init?.status ?? 200 };
}
