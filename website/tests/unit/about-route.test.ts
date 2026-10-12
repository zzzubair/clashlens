import { expect, it } from "vitest";

import { loader } from "../../app/routes/about";

it("sends an old About link to the home page for good", () => {
  let thrown: unknown;
  try {
    loader();
  } catch (error) {
    thrown = error;
  }
  expect(thrown).toBeInstanceOf(Response);
  expect((thrown as Response).status).toBe(301);
  expect((thrown as Response).headers.get("Location")).toBe("/");
});
