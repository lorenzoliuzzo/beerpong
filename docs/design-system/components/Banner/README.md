Banners tell a couple what the page means for them right now; one or two at the top of the page.

Variants by urgency: `.hot` (accent, pulsing: "Tocca a voi"), `.warm` (next call), plain (later, eliminated), `.todo` (a chore or a link: referee duty, questionnaire, cup split), `.gold` (champions). Parts: `.label` (eyebrow), `.headline`, then one line.

**Roast:** the best component in the app, used inconsistently. The profile (`squadra.html`) writes `banner live` and `banner next`, which have no CSS, so "in campo ora" renders as a plain card there while the Tavoli page shows the same state as `.hot`. `.hot` and `.todo` are both orange.
