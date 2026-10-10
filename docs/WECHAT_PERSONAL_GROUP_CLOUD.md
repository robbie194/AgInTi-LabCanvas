# OverTree personal-WeChat coaching groups

The optional `wechat_personal_group` transport adds a personal-WeChat group as
a coaching entrance into the existing OverTree Cloud Agent. WeChat remains a
transport only: Cloud owns the user, project, session, agent run, file intake,
and result. The existing WeChat Customer Service and WeCom transports keep
their separate routes and credentials.

## Identity and isolation

One operator-owned personal WeChat account can join several customer groups.
Each exact group ID has one Cloud binding and maps to one customer's project
and session. Each group gets its own local monitor state file and Cloud
binding, so customer A's history, files, mode, permissions, and Agent session
do not enter customer B's group. Customer and registered support-member rows
are labelled separately before they enter the same OverTree session.

The WeChat account itself is still the operator's account. Every customer sees
the same account name, avatar, and WeChat ID. This is logical Agent isolation,
not a different WeChat identity for each customer. LabCanvas uses the visible
official desktop client; this is personal-account automation, not an official
Tencent Bot API, and it cannot be described as zero-risk.

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
```

`WECHAT_PERSONAL_GROUP_CLOUD_URL` must be the private Cloud address reachable
from LabCanvas (for example, a local SSH-tunnel endpoint), not a public route
that bypasses the reverse-proxy block. Add only the exact tested `chatroom_id`
to `WECHAT_PERSONAL_GROUP_ALLOWLIST`; use comma-separated IDs only when each
one has its own monitor configuration and state file.

The ignored per-group direct-chat config needs these fields in addition to its
normal local WeChat database and GUI settings:

```json
{
  "chat_name": "Customer Coaching",
  "chatroom_id": "<exact-chatroom-id>",
  "self_wxid": "<operator-account-wxid>",
  "state_path": "agentic_tools/wechat_gui_agent/.private/customer-coaching.state.json",
  "send_target": {
    "name": "Customer Coaching",
    "expected_title": "Customer Coaching"
  },
  "personal_group_cloud": {
    "enabled": true,
    "support_member_sha256": [],
    "timeout_seconds": 20,
    "media_sync_timeout_seconds": 30
  }
}
```

Do not copy the example values literally. The monitor requires the exact Agent
`self_wxid` so its own group replies do not become new user requests. The GUI
sender requires the exact expected group title and remains guarded by its
existing visible-client/title checks. Keep one unique `state_path` per group;
the adapter enforces that it is inside `.private`, records its owning group,
and refuses to reuse it for another group.
For staff role attribution, place SHA-256 hashes of registered support-member
WeChat IDs in `support_member_sha256`; never put raw support IDs in the checked-in
file.

Start the existing direct monitor for that private group config using its
normal `--loop --send` command. When the Cloud and local flags are false, the
personal group path refuses to start; an enabled config with an unlisted group
also fails closed. When enabled, that monitor sends all eligible human group
messages to the bound Cloud session and does not run the LabCanvas local Agent
for those messages. Every other configured chat keeps its existing local
behavior.

## Binding a group

1. In OverTree Cloud's `/wechat` page, choose the customer's project, the
   intended existing session (or create a dedicated one), select **微信辅导群**,
   and create a one-time link code. Customer Service remains the separate
   self-service entrance; it does not provision a unique WeChat account for
   every Cloud session.
2. Create a new ordinary WeChat group for that customer. Add the customer,
   your operator account, and the support people who should be visible.
3. Start the exact-group LabCanvas monitor and enable that exact group ID in
   its allowlist. Send the one-time code in the group.
4. Confirm that the Cloud binding appears and that a customer message, a
   support-member message, and a result each remain in that one Cloud session.
5. Test a small image and file, duplicate/restarted message handling, a
   temporary Cloud outage, and a send-title mismatch before enabling any real
   customer group.

Disable both feature flags immediately if the wrong group opens, self-replies
loop, a different group's file appears, the WeChat client requests security
verification, or the personal account shows an abnormal-login warning.

## Current implementation boundary

The LabCanvas direct monitor has an isolated Cloud dispatch branch, exact
allowlist checks, HMAC-signed JSON and binary-upload calls, per-sender role
labels, result polling, a durable retry outbox, and source-token-scoped media
intake. Automated tests cover sender routing, attachment upload requests,
binding-code recovery, result delivery, and refusal to route other groups.

This is not yet enabled or deployed for a real user group. The Cloud flag stays
false until a separate test device, a real private route, and the acceptance
checks above have been exercised with your personal WeChat account. We cannot
verify group membership, media availability, Cloud firewall rules, or Tencent
account status from this repository alone. The Enterprise WeChat Customer
Service Gateway remains a separate self-service transport; do not point it at
the personal group adapter or share its credentials/state with LabCanvas.
