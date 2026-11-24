import { redirect } from "react-router";

export function loader(): never {
  throw redirect("/", 301);
}
