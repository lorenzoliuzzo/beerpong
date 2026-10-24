Buttons are filled `accent` by default, `.ghost` for secondary, `.danger` for destructive, `.link` for inline text actions, and `.ref-go` for the referee's start / confirm.

- Primary submit of a form: `button.wide` (full width, 54px), last thing in the form.
- Row actions: `.small` (38px, under the 44px rule the rest of the app follows).
- Referee: `.ref-go` (72px, `ok` fill, radius 16px) and `.ref-miss` (60px, `err` fill).
- Markup: `<button>` or `<a class="btn">`; the consumer supplies the label and, for destructive actions, an `onsubmit="return confirm(...)"`.

**Roast:** white on `accent` is 4.3:1 in light. `.ref-go` is white on `ok`: 1.75:1 in dark, on the button the referee presses most.
