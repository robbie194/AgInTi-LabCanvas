# WeChat preview refresh and UTF-8 clipboard

The browser showed an old unnamed-group frame while a direct X11 screenshot
already showed the renamed native group. The WeChat wrapper now opts into
x11vnc pixel polling (`--no-xdamage`); other virtual desktops retain their
existing defaults. Startup applies this to reused relays too. Native current
titles remain authoritative for admission; rename notices are insufficient.

The lite client lacked clipboard controls. The full client transferred ASCII
but corrupted Chinese bytes on this installation. A loopback-only preview
wrapper now embeds full noVNC and provides explicit UTF-8 transfers through
xclip. The 6108 HTTP bridge enforces exact Host, Origin and an in-page token,
uses bounded text/subprocess operations, and never logs clipboard data. Its
launcher reuses a healthy service, preserves the signed-in client, and retains
a direct noVNC fallback. Monitor Copy still shares the remote clipboard.

Validation:

- 41 focused tests passed: preview API/security/UTF-8 boundaries, desktop
  lifecycle and WeChat health. Python compilation, shell syntax and diff checks
  passed.
- Live browser → X11 → browser roundtrip preserved Chinese and an emoji. The
  browser reported successful transfer and successful local copy. The actual
  renamed title was visible after refresh. No chat message was sent.
- Full Python suite ran 2,229 tests but did not pass (11 failures, 389 errors,
  10 skips). This sparse checkout omits required tracked modules/assets; the
  environment also rejects bwrap user namespaces. One desktop assertion still
  expects a hardcoded client path that is absent even in the prior HEAD.
- npm test stops at a missing sparse-checkout bin/labcanvas.js. The full-suite
  failures are outside this preview change; no all-suite success is claimed.

Deployment copies only the preview/launcher/status changes and documentation
to the isolated local installation. The existing desktop supervisor ensures
the wrapper after restart. No Cloud deployment or group rebinding is involved.
