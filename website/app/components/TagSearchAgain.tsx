import { Form } from "react-router";

/**
 * After the game finds no player for a tag, a search box holding that tag, so
 * the Clasher can fix a typo; sending the same tag back asks the game again.
 */
export function TagSearchAgain({ tag }: { tag: string }) {
  return (
    <Form method="get" action="/" role="search" className="search-form tag-search-again">
      <label htmlFor="tag-search-again">Search for the right tag</label>
      <div className="search-controls">
        <input
          id="tag-search-again"
          name="q"
          type="search"
          defaultValue={tag}
          autoComplete="off"
          autoCapitalize="characters"
          enterKeyHint="search"
        />
        <button type="submit">Search</button>
      </div>
      <input type="hidden" name="recheck" value={tag} />
    </Form>
  );
}
