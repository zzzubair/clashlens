import type { ReactNode } from "react";

/**
 * Renders the small Markdown subset the site's own content files use:
 * `#`–`###` headings, paragraphs, `- ` lists, `[text](url)` links and
 * `**bold**`. HTML comments are notes for the editor and are dropped. Raw HTML
 * is shown as text, and links other than https:// or same-site paths keep
 * only their text.
 */
export function Markdown({ source }: { source: string }) {
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let list: string[] = [];
  const flush = () => {
    if (paragraph.length > 0) {
      blocks.push(<p key={blocks.length}>{inline(paragraph.join(" "))}</p>);
    }
    if (list.length > 0) {
      blocks.push(
        <ul key={blocks.length}>
          {list.map((item, index) => (
            <li key={index}>{inline(item)}</li>
          ))}
        </ul>,
      );
    }
    paragraph = [];
    list = [];
  };
  for (const line of source.replace(/<!--[\s\S]*?-->/g, "").split("\n")) {
    const text = line.trim();
    const heading = /^(#{1,3}) (.+)$/.exec(text);
    if (text === "") {
      flush();
    } else if (heading) {
      flush();
      const Tag = `h${heading[1].length}` as "h1" | "h2" | "h3";
      blocks.push(<Tag key={blocks.length}>{inline(heading[2])}</Tag>);
    } else if (text.startsWith("- ")) {
      if (paragraph.length > 0) flush();
      list.push(text.slice(2));
    } else {
      if (list.length > 0) flush();
      paragraph.push(text);
    }
  }
  flush();
  return <>{blocks}</>;
}

function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    parts.push(text.slice(last, match.index));
    const [, label, href, bold] = match;
    if (bold !== undefined) {
      parts.push(<strong key={match.index}>{bold}</strong>);
    } else if (/^(https:\/\/|\/(?!\/))/.test(href)) {
      parts.push(
        <a key={match.index} href={href}>
          {label}
        </a>,
      );
    } else {
      parts.push(label);
    }
    last = match.index + match[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}
