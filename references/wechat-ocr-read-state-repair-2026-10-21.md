# Personal WeChat OCR ingress repair — 2026-10-21

## Evidence and scope

Compared with upstream `lachlanchen/AgInTi-LabCanvas` master `b0469f8`.
Upstream's primary personal-WeChat ingress uses a persistent message reader and
cursor. The deployed OverTree pilot uses the additional visible-GUI/OCR Cloud
adapter; upstream does not contain `wechat_ocr_group_ingress.py`. An upstream
working transport therefore does not establish acceptance of this OCR adapter.

Reproduced failures:

- Default unconditional unread gating skipped read/current conversations and
  prevented first-binding discovery after the desktop marked a group read.
- Selecting only the latest addressed line dropped consecutive questions.
- Expiring content fingerprints replayed still-visible history after 15 minutes.
- Inbound title failure prevented reconciliation of already pending replies.
- The narrow desktop title crop included the search icon and sender avatar.
- CSV quoting applied to literal Tesseract TSV swallowed rows after a quotation
  mark, exposing numeric OCR records as message text.
- OCR dropped a glyph in a standalone eight-character code. Native Copy read
  the actual code without guessing/replacing characters.
- Right-click on selectable text did not reliably open its full-bubble menu;
  clicking the adjacent padded bubble background did. Failed-menu Escape could
  open the main client's logout confirmation. Cleanup now clicks blank margin.

## Changes

Use read-independent visible-row/current-header discovery, retaining Cloud's
registration/account/owner/live-code validation and exact opened-title guard.
Keep inbound global search disabled. Read addressed bubbles chronologically,
copy complete native text, validate standalone codes, and retain 200 content
fingerprints across restarts. Use native word selection only as a code-specific
fallback, never to replace a full question with one selected word.

Ingress/delivery share compact header geometry. Inbound title failure still
polls Cloud/outbox; outbound delivery independently verifies its own target.
Add content-free heartbeat and private last-scan diagnostics.

## Validation

- 48 OCR ingress regressions, 33 personal-group Cloud adapter tests, and 80 GUI
  sender tests pass (161 total).
- Python compile and whitespace checks pass.
- On the existing isolated desktop, the exact current generated group title
  passed its guard; native reading extracted one complete eight-character
  standalone connection code. Read-only probes did not submit codes/tasks or
  send artificial messages.
- The observed group's earlier code was no longer eligible in the signed Cloud
  registry. A newly generated live code and a real addressed question are still
  needed to verify binding, task completion and return delivery end to end.

## Limits

This GUI ingress has no native message IDs. Identical normalized question text
uses the existing same-group Cloud idempotency key and cannot distinguish two
separate identical bubbles. Only visible addressed text/standalone codes are
read; inbound files/media remain outside this OCR path. Heartbeats and mocked
regressions are not proof of successful group delivery.

Deploy only the two changed Python transport scripts, back up installed files,
and restart only the OCR monitor. Preserve the native client's login, desktop,
worker processes, customer-service channel and Cloud runtime.
