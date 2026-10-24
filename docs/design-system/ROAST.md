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

## Still open

**Admin page is a junk drawer.** Ten sections in one scroll. The danger zone is done; still to do: split "set up the night" from "run the night", and a typed confirmation for the broadcast to every phone.

**Two homes, five rankings.** "Tavoli" is the home and owns Programma, Tabellone and the live pages; Classifica has five views and Eliminazione repeats Tabellone.

**Players still get homework.** The cup-split nag after every match, for rankings the app itself calls skewed.

**Three ways to arrive, four badges.** QR, 4-digit code or the partner's tick, then "Ci sei", "Check-in fatto", "arrivato/a" and "collegato" for overlapping facts.

**One orange does eight jobs.** Brand, actions, links, active tab, "you", badges, referee score and the urgent banner.

**Everything is bold.** 109 declarations at 700–900 against 4 at 400.

**Emoji as an icon system.** About 60 distinct emoji, including the referee's controls.

**No scale.** 13 radii, a dozen spacings, 170 inline `style=""`, about 60 hard-coded colours.

## What to keep

The referee screen, push notifications, the touch ergonomics, the warm palette, the heat-map ramp, and the demo mode.
