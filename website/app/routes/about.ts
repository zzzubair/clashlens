import { redirect } from "react-router";

/** GET /about — the About page is gone, so an old link opens the home page. */
export function loader(): never {
  throw redirect("/", 301);
}
