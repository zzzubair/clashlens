# Blog posts

Posts live in the private repo `zzzubair/clashlens-blog`, not here. The
website reads a copy of that repo on the server, from the folder named by
`CLASHLENS_BLOG_DIR`, and rereads it at most once a minute. With no folder set,
the blog is empty. [Deployment](../../docs/deployment.md#blog-posts) explains
how the server keeps its copy up to date.

The folder holds `posts/<slug>.md`, one Markdown file per post, and `media/`,
the images and data files posts use. The file name is the post's address:
`how-matchmaking-works.md` is published at `/blog/how-matchmaking-works`. Use
lowercase words joined by hyphens. Files whose names start with `_`, such as
`posts/_template.md`, are not posts.

Start the file with front matter between `---` lines:

```markdown
---
title: How matchmaking works
date: 2026-10-01
summary: One line shown in the post list, link previews and the RSS feed.
author: Clash Lens
cover: how-matchmaking-works.png
coverAlt: What the cover image shows, for people who cannot see it.
draft: true
---

The post starts here.
```

`title`, `date` (written `YYYY-MM-DD`) and `summary` are required. `author`,
`cover`, `coverAlt` and `draft` are optional. Lines starting with `#`, and
anything after ` #` on a line, are comments. `cover` is a file name in
`media/`, a site path starting with `/`, or an https address. A cover image
appears at the top of the post and in Discord link previews; 1200 by 630 pixels
suits previews best.

`draft: true` hides the post from the list, the RSS feed and its address for
everyone except the site owner, the sign-in named by `CLASHLENS_BLOG_OWNER`
(for example `google:<subject>`). The owner sees it marked "Draft", and search
engines are told not to index it. Remove the line, or write `draft: false`, to
publish. Files in `media/` are served to anyone who knows their address, drafts'
images included.

Refer to images and files as `../media/<file>`, which the website serves at
`/blog/media/<file>`. A chart `x.png` with a dark version `x-dark.png` next to
it in `media/` shows the light one when the site is in light mode and the dark
one in dark mode; write only `![alt text](../media/x.png)` in the post.

Posts support Markdown headings, lists, links, images, tables, quotes and code
blocks. Raw HTML is removed, not shown. A post with missing or unknown front
matter, a bad date, a bad cover address or a badly formed file name is left
off the site, and the website logs why.
