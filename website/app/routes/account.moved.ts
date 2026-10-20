import { redirect } from "react-router";

/** GET /account/profile and /account/providers — both moved into Settings. */
export function loader(): never {
  throw redirect("/account/settings");
}
