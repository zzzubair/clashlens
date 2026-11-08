import type { SearchResponse, WebsiteErrorResponse } from "./contracts";

export interface PlayerSearchLoaderData {
  query: string;
  search: SearchResponse | null;
  error: WebsiteErrorResponse | null;
}
