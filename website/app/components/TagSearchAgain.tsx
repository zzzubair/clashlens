import { Form } from "react-router";

/**
 * After the game finds no player for a tag, a search box holding that tag, so
 * the Clasher can fix a typo instead of checking the same tag again.
 */
export function TagSearchAgain({ tag }: { tag: string }) {
  return (
    <Form method="get" action="/" role="search" className="search-form">
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
    </Form>
  );
}
