# Accessibility Statement — Sentinel

_Last reviewed: July 2026._

Sentinel's operator UI is built on Streamlit, which provides a reasonable
accessibility baseline; this document records what we do on top of it, what is
known to fall short, and what a procurement review (e.g. a VPAT request)
should be told honestly.

## What the UI does deliberately

- **No color-only signals.** Every decision badge pairs color with text and an
  icon: `✅ ALLOW`, `⛔ REJECT`, `🧑‍⚖️ HUMAN REVIEW`. Severity is always shown
  as a numeric tier, never only as a hue.
- **Descriptive control labels.** Every button, selectbox, radio group, and
  text area carries a visible text label (no icon-only controls); key controls
  additionally carry `help` tooltips.
- **Meaningful image alternatives.** README/document screenshots carry alt
  text describing what the screen shows.
- **Status conveyed as text.** Quarantine, ticket creation, and errors are
  announced via Streamlit alert components (rendered as text blocks), not
  visual styling alone.
- **Structural headings.** Views use `st.title` / `st.subheader`, which render
  as HTML heading elements, giving screen-reader users a document outline.

## Known gaps (inherited or unaddressed)

- **No formal WCAG audit has been performed.** Nothing here claims WCAG 2.1 AA
  conformance; that requires an audit with assistive technology.
- **Contrast** follows Streamlit's default theme and has not been measured
  against WCAG ratios, in light or dark mode.
- **Keyboard navigation** works to the extent Streamlit provides it (tab order
  through widgets); complex widgets (dataframes, selectbox comboboxes) have
  not been verified with keyboard-only use.
- **Screen-reader behavior** of live-updating regions (the streaming agent
  status panel) is unverified; dynamic updates may not be announced.
- **Emoji in headings and trace lines** (🛡️, 🚨, 🎫) are decorative but are
  not marked `aria-hidden`; screen readers will announce them.

## Roadmap

1. Automated checks (axe-core against the running app) in CI.
2. A keyboard-only walkthrough of the review-queue resolution flow — the one
   path a daily reviewer must be able to complete without a mouse.
3. Contrast measurement of both themes; theme overrides where ratios fail.
4. A recorded NVDA/VoiceOver pass over the moderation and queue views.

Issues and feedback: open a repository issue with the `accessibility` label.
