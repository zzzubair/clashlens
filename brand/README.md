# Clash Lens brand

Decided by the owner on 2026-10-08. The brand comes from the logo: its thick dark outline, its yellow-to-orange letters, the ember-red base under them and the gold lens. Since the 2026-10-27 redesign the site around it is calm and cool so the logo and the one main action on each page carry the colour.

## Logo

[`website/public/images/clashlens-wordmark.svg`](../website/public/images/clashlens-wordmark.svg) is the Clash Lens logo. The site header shows it.

It is "CLASH" over "LENS" in chunky tilted block letters. A magnifying glass overlaps the E and N, and two arrows stick into the C and the H. It started from the owner's pencil sketch and is the round 3 wordmark from the branding session.

The file is plain SVG with no embedded images. Each letter shape is defined once in `<defs>` and reused, so letters, colours and spacing can be edited by hand.

The thick dark outline lets it sit on light or dark backgrounds without a box behind it. It gets hard to read below about 64 pixels wide, so small places use the small mark instead.

## Small mark

For the browser tab icon, the phone home-screen icon and the Discord or social avatar.

- **Chosen:** [`assets/mark-cl-block.svg`](../assets/mark-cl-block.svg), "CL" in the wordmark's own letters and colours. [`assets/mark-cl-block-app.svg`](../assets/mark-cl-block-app.svg) is the same on a dark rounded tile. The app icons and the Discord server icon use the sky blue square below instead.
- **Kept as a backup:** [`assets/mark-lens-arrow.svg`](../assets/mark-lens-arrow.svg) and [`assets/mark-lens-arrow-app.svg`](../assets/mark-lens-arrow-app.svg), the gold lens with an arrow behind it.

At 16 pixels the CL mark reads as two orange blocks rather than letters.

[`assets/mark-cl-block-square.svg`](../assets/mark-cl-block-square.svg) is the CL mark, a little smaller, filling a whole sky blue square with soft white sun rays. The owner chose sky blue over the old dark square on 2026-10-12 to make the icon lighter and more fun; the letters are unchanged. The site's home-screen and web app icons are made from it, and so is [`discord-avatar-512.png`](discord-avatar-512.png), which is the Discord server icon. [`assets/mark-cl-block-square-maskable.svg`](../assets/mark-cl-block-square-maskable.svg) is the same square with the letters shrunk to fit inside the circle Android never crops, for Android's own icon shapes. The browser tab icon is `mark-cl-block.svg` on a transparent background. Player-page link previews use [`website/public/images/og-clashlens.png`](../website/public/images/og-clashlens.png), the wordmark on the night background.

## Colours

Changed on 2026-10-27 by the UI/UX redesign: the warm cream and purple-night pages were replaced by a cooler, calmer look so the logo's yellow and orange stand out. The logo itself is unchanged.

Brand colours, the same in both themes:

| Name        | Colour                | Use                                                |
| ----------- | --------------------- | -------------------------------------------------- |
| Night       | `#1a1206`             | Text on the yellow-to-orange fill                  |
| Sun yellow  | `#ffd447`             | Top of the button fill                             |
| Lens orange | `#ff8a1f`             | Bottom of the button fill; focus ring              |
| Ember red   | `#b3261e`             | Never body text                                    |
| Button fill | `#ffd447` → `#ff8a1f` | The one main action on a page, and the current tab |

The site has a light theme (cool daylight grey) and a dark theme (night-sky navy). A first-time visitor gets whichever theme their device is set to; the moon/sun button overrides it and the choice is remembered in that browser.

| Role                 | Light              | Dark                     |
| -------------------- | ------------------ | ------------------------ |
| Page                 | `#eef2f7`          | `#0b1220`                |
| Card                 | `#ffffff`          | `#121b2d`                |
| Panel, table head    | `#f3f6fa`          | `#18233a`                |
| Text                 | `#0d1626`          | `#edf2fa`                |
| Muted text           | `#55627a`          | `#9aabc4`                |
| Link, accent text    | `#a34a07`          | `#ffb547`                |
| Divider, card edge   | `#e1e7ef`          | `#1f2b44`                |
| Trophies, stars      | `#8a5a00`          | `#f6c445`                |
| You, in progress     | `#0b6bb5`          | `#6cc4ff`                |
| Win, trophies gained | `#137a3a`          | `#4cd98b`                |
| Loss, trophies lost  | `#c62828`          | `#ff7b7b`                |
| Top bar background   | white, see-through | page colour, see-through |

Every text colour above reaches at least 4.5:1 contrast on the page, card and panel colours of its theme (the usual minimum for readable text). Text on the yellow-to-orange fill is always Night, which is at least 7.8:1; white on orange is only 2.4:1, so it is never used.

## Fonts

- **Lilita One** for page titles, section headings and big numbers. One weight. SIL Open Font Licence.
- **Nunito** for everything else, with numbers lined up in columns. Card titles are Nunito extra-bold. Labels are sentence case, never ALL CAPS. SIL Open Font Licence.

Both are served from the site itself.

## Look

- **Top bar:** see-through over the page, with the logo, search, theme button and account. On phones and iPads a tab bar sits at the bottom of the screen, and the current tab has a soft orange pill behind its icon.
- **Cards:** a 1px edge in the divider colour and 16px corners, with no hard shadow. Pop-ups and sheets get a soft shadow to lift them off the page.
- **Buttons:** the main action uses the yellow-to-orange fill with Night text; everything else is a quiet panel-coloured button with a thin outline. Controls have 12px corners.
- **Switches and tabs:** a sunken track with the chosen option lifted out of it.
- **Badges and chips:** rounded pills.
- **Phones first:** nothing scrolls sideways except a row of tabs or chips; long lists show a first chunk and a "Show all" button.
- **Consistency:** every page uses the same edges, corners and colours. Nothing gets a one-off style.
