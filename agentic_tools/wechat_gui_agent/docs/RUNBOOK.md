# WeChat GUI Agent Runbook

This workflow controls the native Linux WeChat client through an isolated
virtual desktop. Use it for explicit, small tasks where a human can inspect the
target before sending.
For system-wide reliability invariants, efficiency policy, state meanings, and
recovery playbooks, keep
[`ROBUST_EFFICIENT_OPERATIONS.md`](ROBUST_EFFICIENT_OPERATIONS.md) open while
changing monitor or worker behavior.

## 1. Launch

For a second personal-WeChat account on this same Ubuntu workstation, use the
[local Agent profile guide](../../../docs/WECHAT_PERSONAL_GROUP_LOCAL_PROFILE.md).
It requires a dedicated Linux user and HOME in addition to a separate display;
do not start a second WeChat account inside the normal user's profile. The
profile's noVNC listener binds to localhost. Cloud replies are polled and sent
from the local Agent GUI. Start the local-only Cloud tunnel with
`agentic_tools/wechat_gui_agent/scripts/wechat_cloud_tunnel.sh ensure`; this
flow does not need LazyEdge.

```bash
agentic_tools/wechat_gui_agent/scripts/wechat_virtual_desktop.sh
```

Open the printed noVNC URL. If WeChat shows a QR code, approve login on the
phone. Keep the browser visible while automating so screenshots can be checked.

For normal long-running operation, start the complete stack instead:

```bash
labcanvas wechat stack start --web-port 19474
```

This starts the isolated WeChat desktop, the direct monitor, the worker, media
sync, the LabCanvas web panel, and the daily career/self-analysis scheduler in
tmux-managed sessions.
After reboot, use the installed wrapper so all standard session names and daily
agent settings are recreated consistently:

```bash
~/scripts/create-labcanvas-wechat-after-reboot.sh
```

The wrapper starts or reuses:

| Session | Role |
| --- | --- |
| `labcanvas-wechat` | WeChat GUI, decrypt refresh, per-chat monitors, worker loops, media sync, unlock watchdog, and chat sync. |
| `labcanvas-web` | Local LabCanvas control panel, default `http://127.0.0.1:19474`. |
| `labcanvas-career-daily` | Daily `gpt-5.5`/`xhigh` self-analysis report to `lachlanchan`. |

The desktop launcher disables X11 blanking and disables DPMS when available. If
noVNC looks idle or blank but WeChat should stay logged in, refresh the
keep-awake daemon without restarting WeChat:

```bash
labcanvas wechat desktop keep-awake
```

## 2. Prepare Targets

Create an ignored plan under `.private/`:

```json
{
  "message": "test",
  "targets": [
    {
      "name": "example group",
      "query": "example",
      "expected_title": "example group",
      "result_click": [180, 337],
      "fallback_clicks": [[165, 100], [165, 170]]
    }
  ]
}
```

Use `query` for the search text and `result_click` for the row offset inside the
WeChat window after the search result appears. If a chat is already visible in
the left list, use `open_click` instead. Use `fallback_clicks` for alternate
search result rows; the sender OCR-checks the opened title after every attempt.

For multi-group monitors, confirm state and routing guards:

```bash
labcanvas wechat health --json
```

The fast monitor is tuned for immediate responses: idle polling is 0.8 seconds,
catch-up polling is 0.1 seconds when rows are waiting, the decrypt refresh loop
runs every 1 second, and the fast agent should use `gpt-5.6-sol` with low
reasoning. `gpt-5.5` remains the deeper worker model.
Polling itself is local DB/file work; it only spends Codex tokens when a new
message needs a route decision or reply.

The worker loop chooses its effort separately and defaults to `gpt-5.5`. It
classifies the current user request, not the long reusable queue playbook.
Queued backend tasks start at medium, keep generated-video browser work on
`gpt-5.5` medium by default, use high for CAD/PCB/Blender/file/tool execution,
and use xhigh for full autonomous tasks such as installs, GitHub commit/push,
publishing, ordering, or end-to-end execution. A clear model failure, timeout,
or empty answer retries upward through allowed effort levels up to xhigh.
Missing exact source files, login/CAPTCHA, and approval blockers do not trigger
blind retries.

Agent-turn timeouts use the centralized backend fallback path. If Codex times
out and `agent_fallbacks.fallback_to_aginti=true`, the same chat/role prompt may
retry through AgInTi instead of returning no reply. This is separate from
browser/job monitoring: a Xiaoyunque, LazyEdit, or AutoPublish run that is still
busy should remain a queued state with probes and artifacts, not a fresh
submission.

For ambiguous media tasks, the fast monitor writes an agent route decision into
the queue item. The worker must check that route against the current request
before executing. Publishing to LazyEdit/AutoPublish, Shipinhao, YouTube,
Instagram, or any public platform requires explicit current-message publish/post
intent; prior chat history cannot authorize it.
For `route_kind=generate_video`, the worker writes a generated-video route
contract into the task artifact directory, any subsequent worker/browser agent
must re-check it before acting, and the final result must include a new MP4 path
or an explicit submitted/running/blocked Xiaoyunque status.
That contract carries an `orchestration_routine` with fixed owners and
entrypoints. Use `docs/GENERATED_VIDEO_ROUTINES.md` as the operator reference:
the agent supervises those routines and handles blockers instead of planning a
new workflow each time.
Submitted Xiaoyunque jobs remain in the queue as `generation_waiting`. The
worker records thread/page monitor state, runs short status-probe cycles instead
of one fixed long timeout, derives the next poll from the visible page status,
and suppresses routine progress messages unless
`WECHAT_WORKER_SEND_GENERATION_PROGRESS=1`. When the MP4 is downloaded and
verified, the worker sends it back to the original WeChat chat. LazyEdit
import/process and public publishing are separate opt-in stages.
The route contract stores `stage_permissions` from the current request only, so
old history cannot authorize LazyEdit import or public posting. A request for
generation alone stops after download, verification, and WeChat send-back; a
request that mentions LazyEdit imports/processes with `--no-publish`; a request
that explicitly names publish/post/platforms may proceed to public publish.
For a request that says to generate and publish, the automated system owns the
full chain: generate/monitor, download, verify, return the MP4 to WeChat, submit
to LazyEdit, and publish exactly once to the requested platforms such as
SPH/Shipinhao, Instagram, and YouTube. Do not rely on an operator manually
running the terminal commands outside the worker.
WeChat should be treated as a mirror command box for the persistent worker, not
as the execution engine itself. The supervisor keeps monitors and the worker
alive, Codex sessions are reused per exact chat/role when reasoning is needed,
and long Xiaoyunque rendering is represented by queue state plus CDP probes so
the system can wait for hours without holding an expensive model call open.
Generated-video file delivery is mandatory: the MP4 is sent before the final
done message, recorded in `sent_file_paths`, and a file-send failure leaves the
task in `send_deferred_artifact` or `send_deferred_locked` for retry instead of
marking it complete. LazyEdit/import and public publishing are poststages and
may only enter `generation_poststage_pending` after this delivery gate passes.
If a Codex worker times out before it returns `thread_url`/`page_id`, the worker
discovers active Xiaoyunque pages from Chrome CDP and resumes monitoring from
the matching `thread_id` instead of sending the timeout as the final answer.

Each group keeps reusable Codex sessions by default: `fast` for immediate chat
replies, `route` for intent classification, and `worker` for backend work. The
ignored registry is
`.private/codex_sessions/sessions.local.json`. Session keys are hash-scoped to
the exact chat title; if status shows `legacy_key: true`, back up that registry
and restart the monitor so mixed pre-fix context cannot be resumed. Disable
reuse for debugging with:

```bash
WECHAT_CODEX_REUSE_SESSIONS=0 labcanvas wechat hold restart
```

For WSL/Windows deployments, `agent_backend=claude` can switch the selected
agent calls to Claude Code while leaving the same monitor, worker queue,
routine contracts, and GUI sender in place. See
[`docs/WSL_WINDOWS_DEPLOY.md`](../../../docs/WSL_WINDOWS_DEPLOY.md). Keep Codex
as the default unless the direct-chat config or environment explicitly opts in.

This reloads monitor, worker, and media-sync windows while preserving the
WeChat desktop. Use `labcanvas wechat hold restart-all` only for a deliberate
GUI restart that may require phone confirmation. If the supervisor is not
running, reload fails closed instead of launching WeChat.

When a paper/download site needs login, CAPTCHA, consent, or manual file-save
confirmation, open a browser in the same virtual desktop:

```bash
labcanvas wechat browser-assist --url "https://example.com/download" --json
```

Use the printed noVNC URL to complete the manual step. The worker should wait
for approval before continuing the task.

For `mp.weixin.qq.com` article links that show `环境异常` or `完成验证后继续访问`,
use the worker's read-only source recovery. It retries with a mobile WeChat user
agent, extracts `#js_content`, and caches full article Markdown privately. If
that path is still gated, run the emitted exact-title/account/identity searches
and corroborate against canonical papers, repositories, author pages, or a
trustworthy same-title copy. Do not open/focus a browser or return
`waiting_confirmation` for read-only research. Report the result as
`full_article`, `reconstructed`, or evidence-limited.

## 3. Verify Before Sending

Open targets and record evidence without composing:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_gui_send.py \
  --display :97 \
  --targets-file agentic_tools/wechat_gui_agent/.private/test-targets.local.json
```

Inspect `*-opened.png` under the output directory. The chat title in the right
pane must match the intended target.

## 4. Send

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_gui_send.py \
  --display :97 \
  --targets-file agentic_tools/wechat_gui_agent/.private/test-targets.local.json \
  --send
```

Each send writes a `send_manifest.json`, screenshots, an `events` row, and a
searchable `messages` row.

## 5. Capture Reads

Open the chat, then capture the visible screen:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py capture-read \
  --display :97 \
  --chat "example group"
```

This stores a screenshot and a `screen_ocr` message row. OCR is page-level and
may include sidebar text, so verify important reads from the screenshot.

## 6. Review The Database

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py list --limit 20
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py list-messages --limit 20
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py export-json \
  --output agentic_tools/wechat_gui_agent/.private/wechat_mirror_export.json
```

## 7. External Decrypt Backend

Probe the optional second receive path:

```bash
labcanvas wechat backend probe --json
labcanvas wechat backend decrypt --incremental
```

For local Web UI/SSE inspection, bind the imported upstream monitor to localhost:

```bash
labcanvas wechat backend monitor-web --port 5679
labcanvas wechat backend api-history --port 5679 --json
```

Only run `labcanvas wechat backend find-keys` when keys are missing; it reads the
running WeChat process memory and requires root or `CAP_SYS_PTRACE`.

The persistent supervisor uses this same wrapper from its decrypt refresh pane.
It defaults to `WECHAT_DECRYPT_REFRESH_MODE=incremental` and
`WECHAT_DECRYPT_REFRESH_SMART=1`, so it avoids full decrypt passes while the
source DB/WAL files are unchanged. Use `labcanvas wechat health --json` to check
both external backend readiness and per-group catch-up.

## 8. Group Creation

Open the picker first:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_group_create.py \
  --display :97 \
  --plan agentic_tools/wechat_gui_agent/.private/group-create.local.json
```

Only run with `--create` after member checkboxes and the Finish button are
visually confirmed. Creating a group is a real WeChat action and notifies users.

## Troubleshooting

- If noVNC disappears, check whether the WeChat process exited; rerun
  `wechat_virtual_desktop.sh` to relaunch it on display `:97`.
- If RDP freezes, keep automation isolated on Xvfb and avoid controlling the
  physical desktop display.
- If search opens the wrong row, replace `result_click` with a verified
  `open_click` for the visible chat list or update the offset from a screenshot.
- If OCR is empty, install the needed Tesseract language pack, increase
  `WECHAT_INITIAL_TITLE_WAIT`, or inspect the saved full-page OCR screenshot.
- If a worker result cannot be sent, inspect `send_failed` tasks in
  `.private/wechat_task_queue.jsonl`; the worker records the error instead of
  retrying forever.
- If a task is waiting for approval, use `labcanvas wechat approve` or
  `labcanvas wechat reject`; without a task id the newest waiting task is used.
- If the external decrypt backend reports missing keys, rerun key extraction only
  under explicit local authorization, then run `backend decrypt --incremental`.
