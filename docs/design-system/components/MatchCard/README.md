The match card is the unit of every schedule: meta line (round · 🏓 table, time with a live countdown, LIVE / in-call pill), two team names around the score, optional cup split, then actions.

States: scheduled (`vs` + "· tra 12 min"), live (`live-sc` with a dot), done (score, winner in `ok`), `.mine` (accent border + inset stripe), `.ghost-match` (dashed projection). Macro: `ui.match(m, show_label, mine, actions, labels)` in `_ui.html`.

**Roast:** up to three actions per card (enter result, split cups, referee) and a muted "🧑‍⚖️ Il risultato lo segna l'arbitro" note where there is none.
