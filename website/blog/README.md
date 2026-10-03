# Blog posts

Each post is one Markdown file in this folder. The file name is the post's
address: `how-matchmaking-works.md` is published at `/blog/how-matchmaking-works`.
Use lowercase words joined by hyphens. Publishing a post means adding its file
in a pull request; there is no other step.

Start the file with front matter between `---` lines:

```markdown
---
title: How matchmaking works
date: 2026-10-01
summary: One line shown in the post list, link previews and the RSS feed.
author: Clash Lens
cover: /images/blog/how-matchmaking-works.png
coverAlt: What the cover image shows, for people who cannot see it.
---

The post starts here.
```

`title`, `date` (written `YYYY-MM-DD`) and `summary` are required. `author`,
`cover` and `coverAlt` are optional. Put images in `website/public/images/blog/`
and refer to them as `/images/blog/<file>`. A cover image appears at the top of
the post and in Discord link previews; 1200 by 630 pixels suits previews best.

Posts support Markdown headings, lists, links, images, tables, quotes and code
blocks. Raw HTML is removed, not shown. `npm run test:unit` fails if a post
has missing or unknown front matter, a bad date or a badly formed file name.
