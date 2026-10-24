# Beer Pong

The interface of a one-night beer pong tournament app: every player on their own phone, a referee per table (or couples self-reporting), organisers in a "regia" (control room), and a TV board. This system documents the app **as it is** on branch `torneo` at commit `c9b019b` (FastAPI + Jinja templates + one 1,500-line stylesheet). The next section, **The roast**, is a critical review of the same UI, written to guide the redesign.

## Who it serves

| Surface | Who | Entry | What they need in five seconds |
| --- | --- | --- | --- |
| Phone, player | Each player, by their own phone number | `/checkin` → `/io` (tab "Squadra") | Where do we play, when, against whom |
| Phone, spectator | Anyone | `/` (tab "Tavoli") | What is on each table now, who is winning |
| Phone, referee | One per table, PIN (or a player of an organiser couple) | header "Staff" → `/arbitro` → `/segna/<id>` | Start the match, log every shot, confirm |
| Phone, admin | Organisers, PIN | `/admin` | Payments, arrivals, start, push announcements |
| TV / projector | The room | `/cartellone` | Tables, groups, the bracket, the QR |

## Content fundamentals

- **Language:** Italian, informal. Couples are *voi* ("Tocca a voi"), single players *tu* with a gender slash ("Sei arrivato/a").
- **Vocabulary:** always *coppia* for a team (never *squadra*) and *bicchieri* for cups (never *canestri*). Also *girone* (group), *tavolo* (table; it was *campo* in the first version), *tabellone* (bracket) vs *cartellone* (TV board), *soffio* (blow, rule 5), *tiro della salvezza* (redemption shot), *regia* (control room). The app's visible text follows this; URLs such as `/squadra/<id>` keep the old words.
- **Tone:** pub-friendly and playful. Emoji carry the voice (🍻 sign-offs, 👋, podium medals, push texts, the avatar picker); the interface itself uses icons.
- **Numbers:** tabular numerals; scores with an en dash (`7–5`); litres with a decimal comma (`1,5 L`).

## Visual foundations

- **Ground:** warm paper `bg` (#f6f2ea) with white `surface` cards in light; espresso `bg` (#12100d) in dark. The theme follows the OS (`prefers-color-scheme`); there is no switch. The TV board is always dark (`board-*` tokens).
- **Three jobs, three hues:** `accent` orange is brand and action (links, buttons, active tab, selection); `you` teal marks "this is you"; `live` (the red of `err`) marks what is happening now. Status colours: `ok` green, `warn-soft` yellow (no warn ink), `err` red. The live page adds team colours `t1` blue and `t2` red; couples can also pick their own colour for their avatar and profile stripe. `gold`, `beer` and `voucher` are three near-yellows/oranges for champions, cups and the free-beer coupon. A blue `heat-1`…`heat-7` ramp draws hit maps.
- **Scale:** spacing `sp-half` 2px, `sp-1` 4, `sp-2` 8, `sp-3` 12, `sp-4` 16, `sp-6` 24, `sp-8` 32; radii `radius-sm` 8, `radius` 14, `radius-lg` 20, `radius-pill`. Templates use utility classes on that scale (`mt-3`, `mb-2`, `flex gap-2`, `center`, `plain-link`), not inline styles.
- **Shape:** soft cards (`radius` 14px) with 1px `line` borders and no shadows; pills (`radius-pill`) for status; round avatars; cups as circles in a triangle rack.
- **Type:** the platform system font, no web fonts. Three weights only: `fw-regular` 400 (body, meta), `fw-medium` 600 (labels, pills, buttons, your row), `fw-bold` 700 (headings, scores, big numbers); size does the rest of the hierarchy.
- **Layout:** phone-only, one column, max 560px (`content-max`), 16px gutter, fixed bottom tab bar with three tabs (Squadra / Tavoli / Classifica), sticky top bar with the title and a "Staff" link.
- **Touch:** 44px minimum targets (`touch-min`), 48px inputs at 16px (no iOS zoom), +/− steppers, radio cards, 72px referee buttons (`ref-go-h`).
- **Motion:** pulses for urgency (the "hot" banner, the clock's last 30 s, the voucher), off under `prefers-reduced-motion` for the banner only.

## Usage rules

1. Primary action = filled `accent` button, full width (`.wide`) at the end of a form. Secondary = `.ghost`. Destructive = `.danger` (`err`) or a red link, behind `confirm()`. Referee start/confirm = `.ref-go` (`ok`).
2. The status of a team or row (paid, arrived, live) is a **pill**. The status of *the page for you* (your next match, a to-do) is a **banner** at the top.
3. "Mine" = `you-soft` background, a `you` border or stripe, or the ★ VOI badge (`.badge.you`). Winners = `ok` text. Qualifiers = `ok` stripe; best thirds = `gold` stripe.
4. In the live page team 1 is `t1` (blue, left), team 2 is `t2` (red, right).
5. Body text sits on `bg` or `surface` only. Do not set white text on `ok`, `err`, `t1`, `t2` or `voucher` in dark mode, nor `accent` text on `accent-soft` (see each token's note).

## Iconography

One stroke set: 24px grid, 2px stroke, round caps and joins, `currentColor`, sized to the text (`1.15em`; `.icon.xl` for empty states). The symbols live in `app/static/icons.svg` (one cached file, versioned like style.css) and are drawn with `ui.icon("name")`, or `ico("name")` in live.html's script. The three tab-bar icons are the same style. Emoji are voice, not icons: avatars, push and WhatsApp texts, flash messages, cheers, podium medals. A PWA icon (`icon-192.png`, `icon-512.png`).

## Components

Server-rendered Jinja macros (`app/templates/_ui.html`) and page markup, styled by `app/static/style.css`. The previews here are **static renditions** of that markup (no JS bundle); they render with the app's stylesheet and tokens in the published design system: https://claude.ai/artifact/4EgebuWxE9g9FDJUgbyS4M (private until shared).

## Not synced

- No font files (system stack only) and no logo: none exist. The PWA icons were not copied.
- Jinja macros are not executable here; previews are hand-written static HTML from `_ui.html`, `base.html`, `live.html` and `squadra.html`.
- Not documented as components: the referee table (`segna.html`), the admin "regia" lists, the profile stat grids, the TV board's bracket tree, the push box. Their styles are in bundle.css.
- Couple colours (`service.COLORS`) are not tokens.
