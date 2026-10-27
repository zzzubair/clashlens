# Clash Lens brand

Decided by the owner on 2026-10-08. The brand comes from the logo: its thick dark outline, its yellow-to-orange letters, the ember-red base under them and the gold lens. Since the 2026-10-27 UI/UX redesign the whole site takes its colours from the logo too: warm ember browns, with the yellow-to-orange kept for the one main action on each page.

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

Chosen by the owner on 2026-10-27 ("Warm ember", because it goes with the logo). It replaced a navy look that read as generic. The logo itself is unchanged.

Brand colours, the same in both themes:

| Name        | Colour                | Use                                                |
| ----------- | --------------------- | -------------------------------------------------- |
| Night       | `#1a1206`             | Text on the yellow-to-orange fill                  |
| Sun yellow  | `#ffd447`             | Top of the button fill                             |
| Lens orange | `#ff8a1f`             | Bottom of the button fill; focus ring              |
| Ember red   | `#b3261e`             | Never body text                                    |
| Button fill | `#ffd447` → `#ff8a1f` | The one main action on a page, and the current tab |

The site has a light theme (warm paper) and a dark theme (ember night: brown-blacks like the logo's outline). A first-time visitor gets whichever theme their device is set to; the moon/sun button overrides it and the choice is remembered in that browser.

| Role                 | Light          | Dark                 |
| -------------------- | -------------- | -------------------- |
| Page                 | `#f5efe8`      | `#120d0a`            |
| Card                 | `#fffcf8`      | `#1c1511`            |
| Panel, table head    | `#f8f2ea`      | `#221a15`            |
| Raised (rank plate)  | `#ede3d8`      | `#30251e`            |
| Text                 | `#1c130d`      | `#fbf3ea`            |
| Muted text           | `#6e5e52`      | `#b8a596`            |
| Link, accent, you    | `#b53a0a`      | `#ff9a3c`            |
| Divider, card edge   | `#ebe1d6`      | `#2a201a`            |
| Trophies             | `#865800`      | `#ffc53d`            |
| Win, trophies gained | `#127436`      | `#4ade80`            |
| Loss, trophies lost  | `#c81e1e`      | `#ff6b5b`            |
| Top bar, tab bar     | paper, frosted | ember night, frosted |

Every text colour above reaches at least 4.5:1 contrast on the page, card, panel and raised colours of its theme (the usual minimum for readable text). Text on the yellow-to-orange fill is always Night, which is at least 7.8:1; white on orange is only 2.4:1, so it is never used.

## Fonts

- **Inter** for words: names, headings, labels and body text, from regular to black weight. SIL Open Font Licence.
- **Barlow Condensed** (semi-bold and bold) for scoreboard numbers: trophies, ranks, scores and the Reset clock. Its narrow figures line up in columns and read like a game scoreboard. SIL Open Font Licence.
- Labels are sentence case, never ALL CAPS. The stream overlay keeps **Lilita One** and **Nunito**, its own look on stream.

All are served from the site itself.

## Look

- **Less text:** a label shows only when the number or icon beside it is not clear on its own. Explanations for screen readers stay in the page, hidden from view.
- **Glass, used sparingly:** only the top bar, the floating tab bar on phones and iPads, sheets and dropdowns are frosted. Cards are solid, with a faint top light and a hairline edge.
- **Rank plates:** every rank (Rankings, Crew boards, Armies) sits on a slanted plate; the top three are gold, silver and bronze.
- **Scoreboard numbers:** big numbers use Barlow Condensed; the player page draws the Season as a trophy line, Season stars as proportional bars, and each day's attacks against defenses as a swing bar.
- **Buttons:** the main action uses the yellow-to-orange fill with Night text and a soft orange glow; everything else is a quiet card-coloured button with a thin edge. Controls have 8px corners, cards 12px.
- **Dashboard teaser:** signed-out visitors see a locked example dashboard on the front page, with Create account and Sign in.
- **Phones first:** nothing scrolls sideways except a row of tabs or chips; long lists show a first chunk and a "Show all" button.
- **Consistency:** every page uses the same edges, corners and colours. Nothing gets a one-off style.
