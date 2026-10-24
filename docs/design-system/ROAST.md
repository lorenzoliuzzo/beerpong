# The roast

A critical review of branch `torneo`, updated on branch `claude/beerpong-app-review-96315e` (pushed to GitHub). Items that are already fixed are listed first; what is still open follows.

## Fixed

- **Unstyled "in campo" / "prossima chiamata" banners and badges** on the profile: they now use `.banner.hot` / `.banner.warm`, and `.live-badge-pulse` and `.section-head` have styles.
- **Undefined colours** (`--ink`, `--card` ×9, `--danger`, `--warn`): replaced; a test now fails if any `var()` is undefined.
- **Profile frozen while the invite box was open**: live pages refresh anyway and keep each section open or closed; the "Per fase / Partite fatte" tab survives the refresh.
- **Contrast**: referee ▶ / ✓ / ✗ buttons, LIVE pill, error toast and danger button get a dark ink in dark mode; `accent` #b93d0b and `ok` #237a35 pass AA in light; the voucher uses dark text.
- **PINs**: only the admin PIN exists. Table referee PINs are gone (referees are organizer couples' players, volunteers, or the admin); the admin PIN is behind a reveal and logged only once, when generated.
- **Throttling by IP**: wrong phone numbers and venue codes are counted apart from wrong admin PINs, so the crowd cannot lock the admin out.
- **Phone number as password**: a number opens a couple only on its first phone; the partner enters with the invite QR/link.
- **Questionnaire gate**: login lands on the profile; the two questions are an optional invitation with "Non ora".
- **Self-destructing beer voucher**: it opens and closes as often as needed and is used only when the bar taps "Ritirata".
- **Odds burning CPU**: the win chance is simulated again only when a hit or the clock changes it.
- **Dead CSS** from crowd shot reporting removed.
- **Profile buried the match**: it now reads match banner, arrival and voucher, notifications, referee duty, invite, group, matches; litres, rankings, player stats and heat maps live on `/squadra/<id>/statistiche`.
- **Mixed vocabulary**: everything players read says *coppia* and *bicchieri*; URLs and code names are unchanged.
- **Self-destruct buttons among routine toggles**: rotating the QR key and cancelling the draw live in a "Zona pericolosa" at the bottom of /admin; cancelling needs ANNULLA typed in; "Sposta orari" is no longer pre-filled.
- **God modules**: `service.py` is now the package `app/service/` (core, teams, matches, flow, stats, missing, progress, setup, board, access; none over ~500 lines, re-exported as `service.x`), and `main.py` is a 25-line app factory over `app/web/` (common, public, live, checkin, results, referee, admin), each with its own router.
- **Strings as a state machine**: `Phase`, `Scoring` and `LivePhase` in `app/states.py` are `StrEnum`s; code and templates ask `cfg.phase.is_setup`, `m.mode.by_shots` and the like.
- **Admin page is a junk drawer**: `/admin` is split via segmented navigation into "Preparazione" (info torneo, iscrizioni/coppie, pagamenti, accessi e impostazioni) and "Serata" (tavoli live, presenze/check-in, solo serata, notifiche, zona pericolosa). Broadcast to every phone requires typing `INVIA` to prevent accidental mass pushes.
- **Three ways to arrive, four badges**: arrival and check-in vocabulary consolidated: "📱 collegato" remains strictly for paired phones; player arrivals are "✓ arrivato/a <ora>"; couple presence is uniformly "Check-in fatto ✓" / "✓ presente" once both are at the venue.
- **One orange did eight jobs**: `accent` orange is now only brand and action (links, buttons, active tab, selection). "You" has its own teal `you` / `you-soft` (★ VOI pill, your rows, matches, group and table); live and urgent ("in campo" banner, match now on a table, overtime) use `live`, the red of `err`; scores, counters and group letters are plain text on a neutral fill. A test keeps orange off the "mine" and live rules.
- **Everything was bold** (109 declarations at 700–900, 4 at 400): weights are three tokens, `fw-regular` 400 for body and muted meta, `fw-medium` 600 for labels, pills, buttons and your own row, `fw-bold` 700 for headings, scores and big numbers. No 800 or 900 is left, couple names in tables are regular, and a test rejects any raw weight.
- **Players got homework after every match**: reporting a result no longer jumps to the cup-split form; the profile and Tavoli to-do banners and the admin "bicchieri da dividere" counter are gone. Who sank which cup stays optional, behind a quiet "Chi ha centrato" link on each finished match.
- **No scale** (16 radii, 24 spacings, 170 inline `style=""`, ~35 stray colours): radii are `radius-sm` 8 / `radius` 14 / `radius-lg` 20 / `radius-pill`; spacing is `sp-half` 2 and `sp-1`…`sp-8` (4, 8, 12, 16, 24, 32); stray colours became tokens (`ink-on-bright`, `paper`, `scrim`, `beer-ink`, `voucher`, `board-*`, live pulses via `color-mix`). Inline styles went from 170 to 28 (bar widths and couple colours, plus a few one-off font sizes) via small utility classes (`mt-3`, `flex gap-2`, `center`, `plain-link` …); panels toggle with `hidden`. A test rejects raw spacing, radii and colours.

## Still open

**Two homes, five rankings.** "Tavoli" is the home and owns Programma, Tabellone and the live pages; Classifica has five views and Eliminazione repeats Tabellone.

**Emoji as an icon system.** About 60 distinct emoji, including the referee's controls.

## What to keep

The referee screen, push notifications, the touch ergonomics, the warm palette, the heat-map ramp, and the demo mode.
