# CLAUDE.md

Working agreements for this repository, from its owner. Code conventions, invariants, and
the build, test, and verify workflow are in AGENTS.md; read that first.

## Git

- Commit only when asked. "Commit and push" means commit on `main` and push it. Never
  create a branch or open a pull request unless explicitly asked to.
- Commits tell the story. Reasoning, measurements, and history go in the commit message,
  not in the code or the docs. Several logical commits are fine.
- Other agents work in this repository too. When asked to commit alongside their changes,
  review those changes first (read them in full, run the checks) and give them their own
  commit.

## Code and documentation

- No history, timeline, logbook, changelog, ledger, or diary anywhere in the repository:
  not in the documentation, not in code comments, not in test docstrings. What a change
  fixed, when, and what came before it lives in `git log` only.
- No breadcrumb documents.
- No data, measurements, observations, or narrative in code comments. A comment states
  what the code does and the constraint it satisfies, in a line or two.
- Test docstrings name the property under test, not the incident behind it.
- README.md is for a person deploying the display in their own hangar: human-readable,
  accurate, usable. AGENTS.md is for agents working on the code: clean, concise, factual,
  precise. Neither carries history.
- Scripts contributed from elsewhere (for example by another agent) are adapted to the
  repository's format and conventions before they are added.
- No editor-specific rule files (the owner no longer uses Cursor).
- The project is GPL-2.0: free to use and modify, not to be sold under another license.
  Vendored code must be GPL-2.0 compatible and keep its own license notice.

## The live kiosk

- This Pi is the production kiosk. Building the image and deploying it here is authorized
  without asking. Afterwards, verify that the display actually loaded the new bundle.
- Measure improvements on the live kiosk (screenshots, CPU, GPU memory, DevTools). A change
  that breaks a working view is a regression, whatever it improves elsewhere; keep the
  rendering paths that work.
- Update the documentation screenshots when the display changes.

## Tests

- A test must exercise the actual issue: it fails on the code that had the failure and
  passes on the fix.
- Test what matters most on a kiosk: speed (polls never wait, views stay fresh),
  reliability (outages survived, logged once, caches bounded), and quality (decoding is
  correct).
