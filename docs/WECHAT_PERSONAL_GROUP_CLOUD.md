# OverTree personal-WeChat coaching groups

The optional `wechat_personal_group` transport adds a personal-WeChat group as
a coaching entrance into the existing OverTree Cloud Agent. WeChat remains a
transport only: Cloud owns the user, project, session, agent run, file intake,
and result. The existing WeChat Customer Service and WeCom transports keep
their separate routes and credentials.

> **Current status (2026-10-12): the `OvertreeAI 测试` OCR pilot is running and bound to its selected Cloud project/session. Its binding confirmation was verified in the group after fixing the OCR-only sender.**
> The database-decrypt reader remains unavailable and is not used. The pilot
> reads visible text from explicitly configured groups through the desktop GUI,
> Tesseract OCR, and the existing guarded sender. It never reads the WeChat
> database or process memory. This remains personal-account GUI automation;
> the isolated test profile is enabled only for the exact title `OvertreeAI 测试`
> and the Cloud route still requires a one-time code for a selected project/session.

## Identity and isolation

One operator-owned personal WeChat account can join several customer groups.
Each registered group has a Cloud-issued stable UUID and one unique, fixed
visible title; it maps to one customer's project and session. Each group gets
its own local monitor state file and Cloud
binding, so customer A's history, files, mode, permissions, and Agent session
do not enter customer B's group. Every human participant has the same role:
any member can address the Agent, and Cloud keeps the sender's display name for
conversation attribution without treating customers and support staff as
different permission classes.

The WeChat account itself is still the operator's account. Every customer sees
the same account name, avatar, and WeChat ID. This is logical Agent isolation,
not a different WeChat identity for each customer. The current OCR pilot uses
the visible official desktop client only; it is personal-account automation,
not an official Tencent Bot API, and it cannot be described as zero-risk.

The two entrances have different identities:

- **Enterprise WeChat Customer Service** is an enterprise-owned shared service
  entry. All customers can contact that entry; Cloud maps each external user to
  the project and session they select. This does not create a new enterprise
  WeChat account for every customer or session. For one Customer Service
  account, an external user currently has one active binding. To use another
  session from that same contact, the customer must choose a separate service
  entry or we must add an explicit session selector.
- **Personal WeChat coaching groups** use the operator's one personal WeChat
  identity in multiple customer-created groups. Customers see the same profile
  in every group, but each exact group ID has its own Cloud binding, project,
  session, files, and monitor state. This is the route for a customer plus a
  human coach to talk with OverTree together.

Both routes call the same Cloud project/session/agent services. If both are
bound to the same session, their messages appear in one history. Bind them to
different sessions when those conversations should remain separate.

## Message flow

```text
customer + support person + operator's Agent WeChat
                     │ exact allowlisted group
                     ▼
       LabCanvas local WeChat monitor and GUI sender
                     │ HMAC over private VPN/SSH
                     ▼
     Cloud group binding -> customer project/session
                     │
                     └── reply and short-lived artifact links -> same group
```

The personal-group HMAC secret is separate from the WeChat Customer Service
Gateway and WeCom Smart Bot secrets. Cloud rejects messages unless the group
has an active binding and the configured personal-Agent account ID matches.
Inbound media is copied only when the exact source message yields matching
WeChat media identifiers. If an attachment cannot be located safely, Cloud
receives a clear note that it was not attached; the adapter does not substitute
a nearby file. Cloud result files are returned as short-lived, download-limited
links when the binding allows artifact sharing.

## Cloud configuration

The production defaults remain off:

```dotenv
CLOUD_WECHAT_PERSONAL_GROUP_ENABLED=false
CLOUD_WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID=personal-agent-main
CLOUD_WECHAT_PERSONAL_GROUP_SECRET=<same-random-secret-as-LabCanvas>
```

For the current isolated test, the Cloud personal-group adapter is enabled so
it can accept the binding code. The checked-in defaults above remain off; the
only local OCR config enabled on the test workstation is `OvertreeAI 测试`.

Use a randomly generated secret with at least 32 characters. Put it only in
the Cloud secret store and LabCanvas's private environment file. Do not put it
in a checked-in config, URL, chat, or support ticket. Keep every `/internal/*`
route blocked at the public reverse proxy. LabCanvas must reach Cloud through
a private network or an SSH tunnel; do not use the public Cloud URL to expose
these internal endpoints.

## LabCanvas configuration

Keep environment values in the ignored
`agentic_tools/wechat_gui_agent/.private/wechat_supervisor.local.env` or the
machine's secret manager:

```dotenv
WECHAT_PERSONAL_GROUP_ENABLED=false
WECHAT_PERSONAL_GROUP_ALLOWLIST=
WECHAT_PERSONAL_GROUP_CLOUD_URL=http://127.0.0.1:3220
WECHAT_PERSONAL_GROUP_SHARED_SECRET=<same-random-secret-as-Cloud>
WECHAT_PERSONAL_GROUP_AGENT_ACCOUNT_ID=personal-agent-main
WECHAT_PERSONAL_GROUP_MAX_ATTACHMENT_BYTES=52428800
WECHAT_PERSONAL_GROUP_MAX_ATTACHMENTS_TOTAL_BYTES=52428800
WECHAT_OCR_GROUP_ENABLED=true
WECHAT_OCR_GROUP_REMOTE_SYNC=true
WECHAT_OCR_GROUP_REGISTRY_REFRESH_SECONDS=30
WECHAT_OCR_GROUP_TRIGGER_PREFIXES=@OverTree,＠OverTree
```

`WECHAT_PERSONAL_GROUP_CLOUD_URL` must be the private Cloud address reachable
from LabCanvas (for example, a local SSH-tunnel endpoint), not a public route
that bypasses the reverse-proxy block. When signed registry sync is enabled,
the local scanner downloads only active group registrations from Cloud and
adds those stable UUIDs to its in-memory allowlist. The static allowlist remains
for the legacy pilot and does not need customer IDs added by hand.

New groups are registered on the authenticated OverTree `/wechat` page. A user
creates a group slot there; Cloud assigns a UUID and a unique visible name such
as `OverTree-ABCD`. Customer support creates the ordinary WeChat group, adds
the Agent account, and may append a readable suffix such as
`OverTree-ABCD-论文讨论`. The monitor refreshes the signed registry every 30
seconds and accepts only the registered `OverTree-ABCD` prefix.
The user then selects that slot when generating a code. Cloud stores the slot
UUID with the code, so a code pasted into any other registered group is
rejected and remains usable in its intended group. Do not reuse a registered
prefix for another WeChat group. A user's groups and bindings remain isolated
by Cloud account, group UUID, and per-group local state file.

This registry flow is implemented in the checked-out Cloud and LabCanvas source,
but the Cloud release has not been deployed. Until it is deployed and
`WECHAT_OCR_GROUP_REMOTE_SYNC=true` is enabled on the Agent workstation, only
the already configured `OvertreeAI 测试` pilot is available; new customer groups
still require local registration.

## GUI/OCR group ingress

This opt-in ingress does not change direct chat, Android, WeCom, or local Agent
monitors. The test profile already has a
private config for the exact group `OvertreeAI 测试` at
`/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/OvertreeAI-ocr-group.json`.
Its local routing ID is derived from the title (`ocr-title-7fe7426e296e5304da64c1dc`);
it is not a WeChat database/group ID. The scanner runs every three seconds in
the isolated `overtree-wechat` desktop on `:97` and only forwards the newest
visible line containing `@OverTree` or a standalone 8-character binding code.
The `OvertreeAI 测试` pilot predates group registration and still uses its
title-derived local routing ID; it has not been renamed or migrated.

For manual developer testing only, a private local config can still be created
without `--enable`. Customer-facing groups should use the Cloud group registry
flow above, which avoids editing this local config list.

```bash
sudo -u overtree-wechat env HOME=/home/overtree-wechat \
  WECHAT_AGENT_PRIVATE_DIR=/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private \
  WECHAT_AGENT_ENV_FILE=/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/wechat_supervisor.local.env \
  python3 agentic_tools/wechat_gui_agent/scripts/wechat_ocr_group_config.py \
  --group-name '固定测试群' --trigger '@OverTree'
```

Run the same command again with `--enable`; this marks the private config
active and sets `WECHAT_OCR_GROUP_ENABLED=true`. Keep
`WECHAT_OCR_GROUP_REQUIRE_BADGE=0` and `WECHAT_OCR_DEDUP_SECONDS=900` for the
first test. Then run one guarded pass:

```bash
sudo -u overtree-wechat env HOME=/home/overtree-wechat \
  WECHAT_AGENT_PRIVATE_DIR=/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private \
  WECHAT_AGENT_ENV_FILE=/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/wechat_supervisor.local.env \
  sh -c 'set -a; . "$WECHAT_AGENT_ENV_FILE"; set +a; exec python3 agentic_tools/wechat_gui_agent/scripts/wechat_ocr_group_ingress.py \
  --configs /home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/<group>-ocr-group.json \
  --display :97 --once --send --json'
```

For Cloud-registered groups, set `WECHAT_OCR_GROUP_REMOTE_SYNC=true` in the
private local environment and reload monitors after deploying the matching
Cloud version. The loop discovers those registered titles itself. Every
`WECHAT_OCR_GROUP_INTERVAL` (3 seconds by default), it takes a local screenshot
of the Agent-WeChat desktop, reduces the conversation-list/chat-tail crop to a
quantized 96x64 signature, and compares its hash with the previous sample. An
unchanged screen skips group navigation and OCR. A changed screen, new group
configuration, or the `WECHAT_OCR_GROUP_ACTIVE_RESCAN_SECONDS` safety interval
(180 seconds by default) starts an active pass over exact configured titles.
Already-running Cloud tasks and queued replies continue to be reconciled during
idle passes without opening a chat.

During an active pass, OCR locates the newest configured `@OverTree` line. The
monitor right-clicks inside that line's message bubble, identifies the native
`复制`/`Copy` menu item in a magnified crop beside the click, corroborates it
with a neighboring menu action, and reads the X clipboard only after verifying
it no longer contains a unique sentinel. The copied bubble text is the
submitted message, including its line breaks; OCR text remains the fallback if
the menu or clipboard check fails. A standalone eight-character binding code
continues to be accepted from OCR. The same normalized message is suppressed for 15
minutes by default (`WECHAT_OCR_DEDUP_SECONDS`); an eligible changed message
can enter immediately. The unread red-marker option is still a best-effort
heuristic, not a title or identity check. Inbound images and files are
intentionally not read in this first OCR path. Text replies from OCR groups
click the visible bottom-right Send control in X desktop coordinates and verify
that the draft cleared; this route does not require a native WeChat database
receipt. Other WeChat text sends keep their existing native receipt
verification. Cloud result files still use the existing guarded file-delivery
path.

After Cloud registry sync is enabled, `wechat_agent_profile.sh reload-monitors`
starts or reloads the `ocr-group` supervisor window. To roll back registry
sync, set `WECHAT_OCR_GROUP_REMOTE_SYNC=false` and reload monitors. To stop all
OCR groups, set `WECHAT_OCR_GROUP_ENABLED=false` and reload monitors. The
ordinary monitors remain running.

Start the OCR supervisor window only after the one-shot check. When the Cloud
and local flags are false, the personal group path refuses to start; an enabled
config with an unlisted group also fails closed. When enabled, the OCR monitor
sends only addressed text and binding codes to the bound Cloud session and does
not run the LabCanvas local Agent for those messages. Every other configured
chat keeps its existing local behavior.

## Binding a group

Follow these steps only after the OCR one-shot pass has validated the exact
group title and Cloud route.

1. In OverTree `/wechat`, select **个人微信辅导群** and create a dedicated
   four-character group name. Send the generated prefix to support.
2. Support creates a normal WeChat group, adds the customer, support members,
   and the Agent account, then names it with the generated prefix. A suffix may
   be added for human readability, for example `OverTree-ABCD-论文讨论`.
3. On `/wechat`, choose the registered group, project/session, and idle period;
   generate a one-time code. Send that code only in this group. Cloud binds it
   only to the group's UUID. Customer Service remains a separate entrance.
4. The monitor discovers the registration on its next refresh, validates the
   registered visible prefix, and replies with a binding confirmation. Later, a new
   code for this same group switches only its project/session. Other groups'
   messages, files, and session state remain isolated.
5. Verify one addressed question and the returned text in the same group.
   Inbound images/files are not part of this OCR version; Cloud-generated
   result files still use the existing guarded file path.

New personal-group bindings default to automatic disconnect after 24 hours
without a Cloud conversation. The Cloud UI also offers 3 days, 7 days, or no
automatic disconnect. A running Cloud task keeps the binding alive. After idle
expiry, the first message that @mentions the Agent receives a reconnect
instruction and is not sent to the old session; the customer generates a new
one-time code and posts it in the same group to select a project/session again.

Disable both feature flags immediately if the wrong group opens, self-replies
loop, a different group's file appears, the WeChat client requests security
verification, or the personal account shows an abnormal-login warning.

## Current implementation boundary

Cloud source now persists registered group slots, associates one-time codes
with the selected slot UUID, and exposes the signed active-title registry only
to the configured Agent account. LabCanvas source syncs those registrations,
uses stable per-group state files, and keeps the legacy pilot config working.
The Cloud registry release is not live yet. OCR text replies use visible
Send-button delivery with composer-clear verification; native database
receipts remain in use for other WeChat routes. Tests cover sender routing,
receipt-free OCR delivery, group registry sync, group-scoped code checks, Cloud
request signing, deduplication, and feature gating. OCR accuracy still depends
on the installed WeChat theme, resolution, and configured crop region.

The `OvertreeAI 测试` OCR loop is enabled and its selected project/session
binding is active. On 2026-10-12 the binding confirmation was successfully sent:
the visible sender returned `sent-visible`, the composer cleared, the
confirmation was recognized in the sent screenshot, and the retry outbox became
empty. Six stale invalid-code notices were removed from the private retry queue
after a private state backup; they were not sent. A subsequent Cloud result entered the local outbox and was sent with
`sent-visible`; both the pending queue and outbox then became empty. Recipient
read status and file delivery have not been verified. The current OCR pilot does not read inbound
images or files. We cannot verify group membership, media availability, Cloud
firewall rules, or Tencent account status from this repository alone. The
Enterprise WeChat Customer Service Gateway remains a separate self-service
transport; do not point it at the personal group adapter or share its
credentials/state with LabCanvas.
