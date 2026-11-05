import { VIEW_AS_PARAM } from "../lib/dashboard";

/**
 * The site owner's View as box: a player tag opens that player's dashboard,
 * read only. Only the owner's dashboard shows it, and only the owner's
 * dashboard reads what it sends.
 */
export function ViewAsForm() {
  return (
    <form className="dash-view-as-form" method="get" action="/">
      <label htmlFor="dash-view-as-tag">View as player</label>
      <input
        id="dash-view-as-tag"
        name={VIEW_AS_PARAM}
        placeholder="#TAG"
        required
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
      />
      <button className="button button-secondary" type="submit">
        View
      </button>
    </form>
  );
}

/** While View as is on: whose dashboard this is, that it is read only, and Exit. */
export function ViewAsBar({
  viewAs,
}: {
  viewAs: { tag: string; name: string | null } | "not-found";
}) {
  return (
    <>
      <section className="notice notice-warning dash-view-as" aria-label="View as">
        <p role="status">
          {viewAs === "not-found" ? (
            <b>Player not found</b>
          ) : (
            <>
              Viewing as{" "}
              <b>
                <bdi>{viewAs.name ?? viewAs.tag}</bdi>
              </b>{" "}
              ({viewAs.tag}) – read only
            </>
          )}
        </p>
        <a className="button button-secondary" href="/">
          Exit
        </a>
      </section>
      <ViewAsForm />
    </>
  );
}
