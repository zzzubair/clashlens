# Clash Lens brand

Decided by the owner on 2026-10-08. The brand comes from the logo: its thick dark outline, its yellow-to-orange letters, the ember-red base under them and the gold lens. It should run through the whole site, not sit in a corner: bold, fun and poppy, but consistent from page to page.

## Logo

[`website/public/images/clashlens-wordmark.svg`](../website/public/images/clashlens-wordmark.svg) is the Clash Lens logo. The site header shows it.

It is "CLASH" over "LENS" in chunky tilted block letters. A magnifying glass overlaps the E and N, and two arrows stick into the C and the H. It started from the owner's pencil sketch and is the round 3 wordmark from the branding session.

The file is plain SVG with no embedded images. Each letter shape is defined once in `<defs>` and reused, so letters, colours and spacing can be edited by hand.

The thick dark outline lets it sit on light or dark backgrounds without a box behind it. It gets hard to read below about 64 pixels wide, so small places use the small mark instead.

## Small mark

For the browser tab icon, the phone home-screen icon and the Discord or social avatar.

- **Chosen:** [`assets/mark-cl-block.svg`](../assets/mark-cl-block.svg), "CL" in the wordmark's own letters and colours. [`assets/mark-cl-block-app.svg`](../assets/mark-cl-block-app.svg) is the same on a dark rounded tile, for the app icon and avatars.
- **Kept as a backup:** [`assets/mark-lens-arrow.svg`](../assets/mark-lens-arrow.svg) and [`assets/mark-lens-arrow-app.svg`](../assets/mark-lens-arrow-app.svg), the gold lens with an arrow behind it.

At 16 pixels the CL mark reads as two orange blocks rather than letters.

[`assets/mark-cl-block-square.svg`](../assets/mark-cl-block-square.svg) is the CL mark filling a whole dark square. The site's home-screen and web app icons are made from it, and so is [`discord-avatar-512.png`](discord-avatar-512.png), ready to upload as the Discord server or bot avatar. The browser tab icon is `mark-cl-block.svg` on a transparent background. Player-page link previews use [`website/public/images/og-clashlens.png`](../website/public/images/og-clashlens.png), the wordmark on the night background.

## Colours

Brand colours, the same in both themes:

| Name            | Colour                | Use                                                    |
| --------------- | --------------------- | ------------------------------------------------------ |
| Outline / night | `#1d1426`             | Logo outline, top bar, outlines, text on yellow/orange |
| Sun yellow      | `#ffd447`             | Top of the button fill; warning badges                 |
| Lens orange     | `#ff8a1f`             | Bottom of the button fill; focus ring; top bar edge    |
| Ember red       | `#b3261e`             | Edge under letters and buttons; never body text        |
| Lens gold       | `#f2c641`             | Trophies, first place                                  |
| Button fill     | `#ffd447` → `#ff8a1f` | Primary buttons and the current page in the top bar    |

The site has a light theme (warm cream) and a dark theme (night). A first-time visitor gets whichever theme their device is set to; the moon/sun button overrides it and the choice is remembered in that browser.

| Role               | Light     | Dark      |
| ------------------ | --------- | --------- |
| Page               | `#fff8ec` | `#120d17` |
| Card               | `#ffffff` | `#1d1626` |
| Panel, table head  | `#fff0d6` | `#2a2034` |
| Text               | `#1d1426` | `#fff4e2` |
| Muted text         | `#6a5848` | `#bcaa98` |
| Link, accent text  | `#b8460c` | `#ffb547` |
| Divider            | `#f0dfc4` | `#33283f` |
| Card outline       | `#1d1426` | `#000000` |
| Success            | `#1f7a33` | `#69db7c` |
| Danger             | `#c92a2a` | `#ff8787` |
| Top bar background | `#1d1426` | `#0b0810` |

Every text colour above reaches at least 5:1 contrast on the backgrounds it is used on (the usual minimum for readable text is 4.5:1). Text on the yellow-to-orange fill is always `#1d1426`; white on orange is only 2.4:1, so it is never used.

## Fonts

- **Lilita One** for page titles, section headings and big numbers. One weight. SIL Open Font Licence.
- **Nunito** for everything else, with numbers lined up in columns in tables. SIL Open Font Licence.

Both are served from the site itself, like the current fonts.

## Look

- **Top bar:** dark in both themes, with a 3px orange line under it. The current page is a yellow-to-orange pill with a dark outline.
- **Cards and buttons:** a 2px dark outline and a hard shadow straight down, 4px under cards and 3px under buttons, like the logo's sticker look. Corners round at 10px on buttons and inputs, 14px on cards.
- **Primary buttons:** the yellow-to-orange fill with dark text. Secondary buttons: card colour with the same outline and shadow.
- **Badges:** rounded pills with a 2px outline in the badge's own colour.
- **Consistency:** every page uses the same outline thickness, shadow depth and corner rounding. Nothing gets a one-off style.
