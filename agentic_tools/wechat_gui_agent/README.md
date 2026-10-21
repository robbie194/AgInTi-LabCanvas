# WeChat GUI Agent

This tool automates the native Linux WeChat client through a clean Xvfb/noVNC
desktop. It uses visible GUI control instead of private protocol hooks, records
evidence screenshots, and mirrors send/read events into a local SQLite database.

For the complete operator map of CLI commands, tmux sessions, scripts, private
state files, route guards, media sync, worker tasks, skills, and safety rules,
read [docs/FULL_CONTROL_MANUAL.md](docs/FULL_CONTROL_MANUAL.md).
For the reliability and efficiency contract used by future agents, read
[docs/ROBUST_EFFICIENT_OPERATIONS.md](docs/ROBUST_EFFICIENT_OPERATIONS.md).
For the transport repair runbook covering the `wechat_login_required` and
`wecom_login_required` health-guard issue codes, including how to restart an
exact dead or stalled tmux window, resume a durable task, and clear an orphaned
process only after proving it is orphaned, read
[docs/TRANSPORT_REPAIR_RUNBOOK.md](docs/TRANSPORT_REPAIR_RUNBOOK.md).
The runbook also documents the `wechat_login_required` recovery sequence:
confirm the exact tmux window is dead or stalled, restart only that window via
`labcanvas wechat hold start` / `labcanvas wechat stack start`, then verify the
health probe returns to `ok` before resuming any queued durable task.
For the reusable routine registry that keeps agents supervising known workflows
instead of inventing new ones, read
[docs/ROUTINE_ORCHESTRATOR.md](docs/ROUTINE_ORCHESTRATOR.md).

## Start The Desktop

Use the wrapper for the shared WeChat desktop:

```bash
agentic_tools/wechat_gui_agent/scripts/wechat_virtual_desktop.sh
# or, through the installed/source LabCanvas CLI:
labcanvas wechat desktop start
```

Manual launch:

```bash
agentic_tools/virtual_desktop/launch_virtual_desktop.sh \
  --name wechat \
  --display :97 \
  --screen 1920x1080x24 \
  --vnc-port 5917 \
  --novnc-port 6107 \
  --app-match /opt/wechat/wechat \
  -- /usr/bin/wechat
```

Open:

```text
http://127.0.0.1:6107/vnc.html?host=127.0.0.1&port=6107&autoconnect=1&resize=scale
```

For normal operation use `wechat_virtual_desktop.sh ensure` and open
`http://127.0.0.1:6108/`. Its **复制与粘贴** panel preserves UTF-8 text:

- Local → remote: paste into the panel, click **传入远端**, then Ctrl+V in WeChat.
- Remote → local: copy in WeChat, click **复制到本机**. If the browser denies
  clipboard permission, the panel selects the text for Ctrl+C instead.

The preview bridge binds only to loopback. Clipboard access requires an
in-page capability token, an exact same-origin request and a loopback Host;
GET requests never return clipboard contents. It accepts at most 1 MiB of
UTF-8 text and does not send chat messages. `WECHAT_PREVIEW_PORT` defaults to
`WECHAT_NOVNC_PORT + 1`. The direct full noVNC link above remains a fallback,
but its legacy clipboard can corrupt non-Latin text. Browser Ctrl+V over the
canvas alone does not synchronize clipboards. Monitor/native Copy operations
share the remote clipboard; coordinate manual maintenance with the GUI lock.

The Linux WeChat client is single-instance. If it is already running on another
desktop, close that instance before launching it on the virtual display.
The WeChat wrapper disables XDamage-based frame skipping to avoid stale Qt
window previews on Xvfb; other virtual desktops retain their existing default.
The wrapper keeps the isolated X11 desktop awake with `xset`, so noVNC should
not blank during long-running monitoring. Apply or refresh this without
restarting WeChat:

```bash
labcanvas wechat desktop keep-awake
```

The full supervisor also starts `unlock-watchdog` by default. It detects the
official Linux WeChat locked screen, then uses the already-authorized Android
phone's normal mobile WeChat controls to unlock the desktop session and flush
deferred sends:

```bash
labcanvas wechat unlock-watchdog once --serial <ADB_SERIAL> --flush-deferred
labcanvas wechat unlock-watchdog start --serial <ADB_SERIAL> --flush-deferred
```

Set `WECHAT_UNLOCK_WATCHDOG=0` before starting the supervisor to disable this
phone-side watchdog. It does not bypass phone credentials or private WeChat
protocols.

## Persistent ChatOps

Create ignored private configs first:

```bash
labcanvas wechat init-config --chat "example group"
```

Fill the direct config under `.private/` with the decrypted message table and
self account ID. Then start the full supervisor:

```bash
labcanvas wechat hold start
labcanvas wechat stack start --web-port 19474
labcanvas wechat status
labcanvas wechat control-map --json
labcanvas wechat routines --json
tmux attach -t labcanvas-wechat
```

The supervisor keeps a virtual desktop/decrypt window, one fast direct-monitor
window per configured group, plus worker, media-sync, and unlock-watchdog
windows. Monitor, worker, media, and watchdog processes restart automatically
if they exit. Incoming
mentions can get an immediate ACK while longer work is queued for
`wechat_task_worker.py`, which can send a final message plus PDFs/images/files
back through the official WeChat GUI.
Each queued backend task stores a named routine contract from
`wechat_routines.py`; the worker writes `routine_contract.json` and
`routine_contract.md` before invoking Codex, then supervises that routine's
stages and artifact gates.

### Career / Writing / Money Agent

`MEMO写作—外语—挣钱` and the `lachlanchan` DM have a dedicated
`career_strategy` route. Messages about what to write, career direction,
talent, monetization, opportunities, GitHub/lazying.art positioning, or
practical money-making experiments are routed to a worker that can inspect
private memory, local repos, and current web/GitHub context before replying.

Run a one-shot report:

```bash
PYTHONPATH=src python -m agenticapp wechat career-agent once --json
```

Run it every morning in tmux and send the sanitized report to `lachlanchan`:

```bash
PYTHONPATH=src python -m agenticapp wechat career-agent start \
  --send --attach-report --morning-time 08:30
```

The full evidence report is written under ignored `.private/output/`; the
shareable attachment is written under ignored `output/wechat_strategy/`. Every
run also creates a private trace bundle under
`.private/output/career_daily/runs/YYYY-MM-DD-HHMMSS/` with the exact agent
prompt, memory snapshot, project surface, identity/repo evidence, raw agent
result metadata, private report, sanitized share report, and `manifest.json`.
See `docs/CAREER_SELF_ANALYSIS_AGENT.md` for the full method.

Use `labcanvas wechat hold restart` or `labcanvas wechat hold reload-workers`
after code changes; these commands do not restart the WeChat desktop. Use
`restart-all` only if you intentionally want the official client to close and
reopen, which may require mobile confirmation. If the supervisor is not running,
reload fails instead of launching WeChat unexpectedly.

The direct monitor uses recent full chat history, not just the newest polling
batch. Prompt context labels the latest row and bot/self replies, so a bare
mention, repeated message, or fragment such as "same one" can refer to the
previous request without repeating the same answer. By default,
`coalesce_new_messages` makes a burst of incoming rows produce one reply to the
latest actionable turn while marking earlier actionable rows as `FOCUS`, so
EchoMind analyzes every sentence in the burst and research tasks include every
instruction. Queued tasks include recent synced file paths so requests like
"summarize this PDF" can resolve to the latest downloaded PDF.
WeChat quote/reply rows are decoded as `quote_reply`: the reply title is treated
as the current command and the referenced message is included as quoted context.

For low-latency chatops, the supervisor defaults to:

- `WECHAT_DIRECT_POLL_SECONDS=0.8` for idle direct DB polling.
- `WECHAT_DIRECT_CATCHUP_POLL_SECONDS=0.1` when rows are waiting.
- `WECHAT_DECRYPT_REFRESH_INTERVAL=1` for the shared decrypted cache refresh.
- `gpt-5.5` with `medium` reasoning and a 60 second timeout for the fast agent.

If commands are sent from the same logged-in account on mobile, set
`allow_human_self_messages=true` and `self_message_policy=human_commands` while
keeping `ignore_self_messages=true`, `respond_to_self=false`,
`self_messages_text_only=true`, and `ignore_probable_bot_self_replies=true`.
This allows human self text commands but blocks bot replies and self-sent files
from recursively triggering new tasks.

To monitor multiple groups, create one ignored direct config per group and set
`WECHAT_DIRECT_CONFIGS` in `.private/wechat_supervisor.local.env`:

```bash
WECHAT_DIRECT_CONFIGS='/path/to/group-a-direct.json,/path/to/group-b-direct.json'
```

Each config should use a distinct `state_path`. Optional `send_target` values
let replies open the correct group before sending, instead of assuming the
visible chat is already correct. Use the health check after edits:

```bash
labcanvas wechat health --json
labcanvas wechat control-map --json
```

It reports each configured group, whether its monitor state has caught up to
the decrypted DB, whether the monitor heartbeat is fresh enough to be `ready`,
and whether the self-message and title-guard protections are enabled.
`chat_quiet=true` or `last_message_old=true` is informational: an idle group
remains healthy when the monitor heartbeat is fresh. `source_stale=true` now
means the monitor heartbeat itself is stale or missing.
It also shows poll timing, Codex model/reasoning settings, and the last loop
timing metrics. Private chatroom IDs, wxids, DB paths, and table names are
omitted.

`control-map` is the implementation guide for robust control. It lists the
supported surfaces: isolated GUI automation, direct receive/mirror state,
same-chat media sync, deterministic workers, event/screenshot observability, and
optional bridge APIs. It also lists blocked approaches: packet/TLS MITM,
private-protocol replay, session extraction, credential theft, login/CAPTCHA
bypass, and cross-chat media guessing. Use Wireshark-style tooling only for
coarse connectivity diagnostics, never for message content or credentials.

For organizer-style groups, enable the private memory sidecar in that group's
direct config:

```json
{
  "chat_purpose": "personal_organizer",
  "respond_to_all": true,
  "organizer": {
    "enabled": true,
    "db_path": "agentic_tools/wechat_gui_agent/.private/wechat_memory.sqlite",
    "capture_unclassified": true,
    "default_tags": ["writing", "foreign-language", "money"],
    "ack_on_save": true,
    "ack_saved_text": "已保存到收件箱。"
  }
}
```

The monitor backs up every new row, then classifies inbound messages into
`source_messages`, `memory_items`, `tags`, and `item_tags`. It can track notes,
memos, todos, groceries, calendar hints, beat-board ideas, writing/language
ideas, money ideas, requests, rich media, and attachments across any number of
groups. The router replies to actionable save/list/summarize/schedule/organize
requests, explicit mentions, and configured shared-object summaries. If
`ack_on_save` is enabled and no worker task was triggered, ordinary saved notes
get a short deterministic receipt without a Codex call.

Use `chat_purpose: "web_clip_inbox"` for a group such as `Shares鏈接`, where the chat
is mainly a read-later stream. Plain URLs, PDFs, images/screenshots,
voice/audio, videos, forwarded webpage cards, mini programs, archives, CAD/PCB
files, YouTube links, 视频号/Shipinhao shares, Bilibili links, contact/location
cards, and other shared objects become records in the same private database,
partitioned by `chat_name`. These shares can also trigger an ACK plus worker
task to summarize or extract the content; summary/list/export requests use the
same fast-router and worker queue.

Voice messages are handled as text when the decrypted media database is
available. The key refresh must include `message/media_0.db`; the monitor then
reads `VoiceInfo`, decodes WeChat SILK with the private decrypt venv's `pilk`,
transcribes the WAV with OpenAI `whisper` or `faster_whisper`, and caches
results in ignored `.private/voice_transcriptions.json`. The default selector
prefers a dedicated multilingual conda environment such as
`~/miniconda3/envs/whisper/bin/python`, then falls back to other ASR-capable
Python installs. EchoMind treats a transcribed voice row as ordinary
language-practice text unless the transcript explicitly asks for backend tools.
Manual check:

```bash
labcanvas wechat voice-transcribe \
  --config agentic_tools/wechat_gui_agent/.private/echomind-direct-chatops.local.json \
  --local-id 121 \
  --backend whisper \
  --python ~/miniconda3/envs/whisper/bin/python \
  --json
```

Raw `aeskey` and `voiceurl` fields are intentionally stripped from prompts.

When a transcribed voice message is routed to the slower worker, the monitor
stores only safe transcript/language/duration fields in the private queue. The
worker writes them to `preflight.audio_intake.agent_context_path` and resumes
the exact chat's persistent Codex session. Ordinary local audio and video
attachments use the same contract: exact same-chat media resolution, `ffprobe`,
audio extraction, Whisper transcription, cache, then agent reasoning. Run that
stage directly with:

```bash
labcanvas wechat audio-intake \
  --input /absolute/path/to/exact-group-video.mp4 \
  --output-dir output/wechat-audio-check \
  --json
```

The model is not invoked while polling an idle chat. It runs only after a real
message becomes a direct response or queued task. A probe/download/ASR failure
is not silence; only readable media with zero audio streams is `no_audio`.

For 视频号/Shipinhao/Finder shares, the worker first tries the exact card media
URL, validates it with `ffprobe`, and transcribes its audio. If that signed URL
has expired, the worker first OCRs the card cover and uses the locally installed
`yt-dlp` module to inspect a bounded public-mirror candidate set. Local
Tesseract/EasyOCR plus bounded Chinese-to-English evidence improve search, and
public captions prefilter candidates before media download. It accepts either a
same-duration content match or an independently corroborated, time-localized
excerpt from a longer source. If no
candidate passes, it scans the bounded recent history of the same guarded chat,
binds a play control to nearby title/author OCR, opens the native player, captures
its `WeChatAppEx` PipeWire stream, and retries the same transcription. Card
duration is a bounded completion signal, so a player that
stays on its final frame does not fail merely because its identity remains
visible. The worker also treats comments as optional summary evidence when
accessible. It should look for viewer prompts such as
`@元宝`, `腾讯元宝`, `英文全文`, `全文`, `总结`, `摘要`, `字幕`, `转写`,
`transcript`, and `summary`, plus other comments with quoted lines, timestamps,
corrections, names, or links. Reading comments is acceptable; posting a comment
or asking Yuanbao from the account requires explicit user confirmation. Only a
readable media probe with zero audio streams is `no_audio`. An HTTP 400, rejected
mirror, missing card, unsupported native player, or missing PipeWire stream is a separate
failure stage and must never be described as a silent video. If the actual
video, comments, transcript, or a reliable public mirror are unavailable, the
worker should not produce a deep analysis; it reports a concise evidence
limitation without opening an external browser or asking for read-only verification.
Run the same routine directly with:

```bash
PYTHONPATH=src python -m agenticapp wechat shipinhao-transcribe \
  --source-text-file output/wechat_worker/TASK/shipinhao_media_transcript/exact-source-card.txt \
  --output-dir output/wechat_worker/TASK/shipinhao_media_transcript \
  --json
```

For an unresolved cover with no useful text, a backend vision turn may add up to
three repeatable `--search-hint` arguments. The script still performs the final
identity check; a guessed speaker or related clip is never accepted by itself.
For the concrete logged-in page/API approach and the local analyzer helper, see
`docs/SHIPINHAO_COMMENT_RESEARCH.md` and
`scripts/shipinhao_comment_intel.py`.

Direct contacts can be monitored with the same config shape as groups. Keep a
unique `chat_name`, `message_table`, and `state_path` for each contact. Set
`bot_identity`, such as `LazyResearch / 懒人科研`, when the contact should hear a
specific assistant persona while remaining separate from the group with the
same persona.

```bash
labcanvas wechat memory init
labcanvas wechat memory summary --chat "写作 外语 挣钱"
```

Install a reusable launcher:

```bash
labcanvas wechat install-user-scripts
~/scripts/labcanvas-wechat-hold.sh start
~/scripts/create-labcanvas-wechat-tmux.sh
~/scripts/create-labcanvas-wechat-stack.sh
```

`stack start` keeps the WeChat supervisor and the LabCanvas browser control
panel alive together. The default web session is `labcanvas-web` on port
`19474`; if that port is busy, the web app uses the next free port and prints
the actual URL.
`labcanvas wechat stack restart` preserves the WeChat GUI and reloads only
worker-side processes plus the web panel. Use `stack restart-all` only for a
deliberate full restart.

## Send Messages

Prepare an ignored target file under `.private/`:

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

Open targets without composing:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_gui_send.py \
  --display :97 \
  --targets-file agentic_tools/wechat_gui_agent/.private/test-targets.local.json
```

Compose without pressing Enter:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_gui_send.py \
  --display :97 \
  --targets-file agentic_tools/wechat_gui_agent/.private/test-targets.local.json \
  --compose-dry-run
```

Send only after screenshots confirm the right chat is open:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_gui_send.py \
  --display :97 \
  --targets-file agentic_tools/wechat_gui_agent/.private/test-targets.local.json \
  --send
```

The script uses `xclip` for Unicode text, `xdotool` for focus/click/keystrokes,
and ImageMagick `import` plus optional `tesseract` for evidence screenshots.
It records screenshots under `output/wechat_gui_agent/YYYY-MM-DD/`.

## Message Mirror

Initialize and inspect the ignored local database:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py init
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py list --limit 20
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py list-messages --limit 20
```

Capture the current visible chat screen with OCR:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py capture-read \
  --display :97 \
  --chat "example group"
```

Export a private JSON snapshot:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_mirror.py export-json \
  --output agentic_tools/wechat_gui_agent/.private/wechat_mirror_export.json
```

See `docs/MIRROR_SCHEMA.md` for the database layout.

## Direct Monitor And Worker

Run one fast direct pass:

```bash
labcanvas wechat monitor once --send
```

Start only the direct monitor:

```bash
labcanvas wechat monitor start
```

Queue slower backend work:

```bash
labcanvas wechat worker enqueue "Download the public PDF for <paper title>"
labcanvas wechat worker once --send
labcanvas wechat queue --json
```

Worker tasks default to `gpt-5.5` and pick effort from the current user request,
not the long reusable queue playbook. Queued backend work starts at `medium`,
keeps generated-video browser work at `gpt-5.5` medium by default, uses `high`
for CAD/PCB/Blender/file/tool execution, and uses `xhigh` for full autonomous
tasks such as installs, GitHub commit/push, publishing, ordering, or "finish
this end-to-end" requests. A timeout, empty result, or clear model failure
retries upward through allowed effort levels up to `xhigh`.
Missing exact sources, login/CAPTCHA, and user confirmation blockers do not
trigger blind retries. If GUI delivery fails, the queue item is marked
`send_failed` with the error instead of retrying indefinitely.

Ambiguous media actions are classified by a route agent before they reach the
worker. The route decision is stored on the queue item, and the worker must
verify it against the current request before acting. Public posting through
LazyEdit/AutoPublish, Shipinhao, YouTube, Instagram, or similar platforms is
allowed only when the current user request explicitly asks to publish/post to a
platform; old chat history is context, not permission.
For `route_kind=generate_video`, the worker writes a route contract under the
task artifact directory, the subsequent Codex/browser worker must re-check that
contract before acting, and the result is rejected unless it contains a new MP4
path or a clear submitted/running/blocked browser status.
The contract also includes an `orchestration_routine`; agents should supervise
that fixed stage list instead of inventing a new implementation path. The
routine is documented in
[`docs/GENERATED_VIDEO_ROUTINES.md`](docs/GENERATED_VIDEO_ROUTINES.md).
Submitted Xiaoyunque jobs are treated as resumable queue work, not as completed
answers. The worker stores the thread/page monitor state, suppresses routine
"still generating" messages by default, runs short deterministic status-probe
cycles, schedules the next poll from page status such as `还需 N 分钟`, `排队`, or
`生成中`, and sends the verified MP4 back to the source WeChat chat when it is
ready. There is no fixed "3 hour then fail" answer; ambiguous or blocked status
is escalated to the worker/agent, while normal rendering waits cheaply. If a
Codex worker times out before returning structured monitor state, the worker
discovers the active Xiaoyunque `thread_id` from Chrome CDP and continues from
that page instead of posting the timeout as the final answer.
LazyEdit import/process is a separate stage and requires an explicit current
request for LazyEdit/import/process. Public posting still requires explicit
current-message publish/post/platform intent.
The generated-video route contract records `stage_permissions` from the current
request only: story/video generation, WeChat send-back, LazyEdit import/process,
and public publish are separate booleans. Old chat history can provide context
but cannot authorize LazyEdit or platform posting.
Capability contract: when the current WeChat request explicitly asks for
generation plus publication, the system must run the whole chain itself:
generate/monitor, download, verify, send the MP4 back, submit to LazyEdit, and
publish exactly once to the requested platforms such as SPH/Shipinhao,
Instagram, and YouTube. This path must not depend on a human manually running
the same commands from a terminal.
Treat WeChat as a mirror command box for the persistent Codex worker. The durable
agent is the running monitor, queue, session registry, and worker supervisor:
messages become queue tasks, Codex worker turns are resumed per chat/role when
reasoning is needed, and long Xiaoyunque waits are held by deterministic queue
state rather than a single fragile multi-hour model call.

Generated-video MP4 delivery is strict by default. When a finished video path is
present, the worker sends that file before the completion text and records it in
the task's sent-file ledger. If the GUI file send fails or WeChat is locked, the
task stays `send_deferred_artifact` or `send_deferred_locked` so the resend loop
can finish delivery later; it is not marked done without returning the MP4 to
the source group. Opening the guarded target chat in dry-run mode is only a
preflight; the file-picker attachment bridge must exit successfully before the
ledger is updated. LazyEdit import/process and public publishing are queued as
`generation_poststage_pending` only after `sent_file_paths` proves that the MP4
was delivered to the source chat.
The same rule applies to returned video/audio files from file-save or download
routes: media files are required artifacts, not optional saved-path notes. To
repair old queue rows that were closed before this invariant, run:

```bash
labcanvas wechat worker repair-artifacts
```

Before work starts, a queue item is claimed as `in_progress` under a file lock.
This prevents a manual `worker once` and the persistent loop from handling the
same request twice. Stale claims are reclaimed after
`WECHAT_WORKER_STALE_IN_PROGRESS_SECONDS` (default: one hour).

Approve or cancel work that is waiting on a confirmation:

```bash
labcanvas wechat approve <task-id> --note "continue with the default option"
labcanvas wechat reject <task-id> --note "do not submit this action"
```

Send a manual message or attachment to the currently visible chat:

```bash
labcanvas wechat send --message "Bridge online."
labcanvas wechat send --file /absolute/path/to/report.pdf
```

Sync recent downloaded files/images into the private workspace:

```bash
labcanvas wechat media-sync --chat "example group" \
  --auto-source \
  --since-minutes 60
```

Synced files are stored under
`.private/downloads/<chat>/<wechat-profile>/<category>/` so images, PDFs, and
videos from different profiles do not collide. The sync scanner includes
`temp/ImageUtils`, where the official WeChat client writes readable JPGs after
an image is opened.

Copy the newest mirrored video from a group or DM into the Nutstore
AutoPublish watcher:

```bash
labcanvas wechat autopublish-video --chat "example group" --sync --json
```

The import command writes through a temporary folder and atomically creates a
`*_COMPLETED` file under `/home/lachlan/Nutstore Files/AutoPublish/AutoPublish`
so the watcher does not see partial copies. Use `--list --json` to inspect
candidate videos and `--source /path/to/video.mp4` for an explicit file. If a
video message is visible in WeChat but no MP4 has been cached yet, use
`--sync --fetch-gui`; the tool opens the chat, clicks the latest visible video,
waits for the official client to cache the MP4, then copies the matched file.
For exact `--message-ref`/`--message-local-id` requests, the GUI path starts at
the newest messages, scans a bounded history, selects the strongest exact
thumbnail match, and retries one lost cache click. Set
`WECHAT_AUTOPUBLISH_VIDEO_VISUAL_PYTHON` only when the automatically discovered
OpenCV-capable Python is not the desired runtime. The downloaded MP4 still has
to satisfy the exact source row's identity or byte-length contract.

## LazyEdit Platform Publishing

Full video publishing is documented in
`agentic_tools/wechat_gui_agent/skills/lazyedit-publish-workflow/SKILL.md`. Use
that workflow when a chat task asks to publish, re-publish, subtitle, monitor, or
send a WeChat video to Shipinhao, YouTube, or Instagram.

The normal path is:

```bash
PYTHONPATH=src python -m agenticapp wechat autopublish-video \
  --chat "example group" \
  --message-local-id VIDEO_LOCAL_ID \
  --sync \
  --fetch-gui \
  --since-minutes 720 \
  --json

cd /home/lachlan/DiskMech/Projects/lazyedit
source ~/miniconda3/etc/profile.d/conda.sh
conda activate lazyedit
python scripts/lazyedit_publish.py \
  --video-id VIDEO_ID \
  --use-current-settings \
  --platforms shipinhao,youtube,instagram \
  --guided-monitor \
  --wait \
  --poll-seconds 10
```

For context-sensitive videos, pass separate files with
`--correction-prompt-file` and `--metadata-prompt-file`. Use `--no-process` only
when reusing an already completed LazyEdit output. If Shipinhao or another
platform needs QR login or manual confirmation, open the isolated browser and
wait for the user rather than bypassing the page.

For an explicit publish request, `--no-publish` is only a quality gate. After
the MP4/ZIP is correct and no manual blocker appears, continue to exactly one
real publish for the requested platforms and report the job ids/status.

The worker has a deterministic fast path for exact WeChat video rows. When
`autopublish-video --message-local-id` succeeds and the chat clearly asks to
publish, the worker waits for the LazyEdit import, runs
`scripts/lazyedit_publish.py` with the generated correction and metadata prompt
files, and monitors the local/remote publish queues before falling back to the
general Codex worker. Disable this path with
`WECHAT_WORKER_DISABLE_DETERMINISTIC_VIDEO_PUBLISH=1` when testing.

When the request comes from WeChat, keep the monitor's
`Video publish/subtitle context bundle` as the correction prompt. It preserves
the coalesced command, quoted message, same-chat media rows, recent context, and
visible metadata so subtitle correction can use the user's actual instructions.
Use a separate concise metadata brief for public title/description/hashtags.
Safe MP4/MOV/audio outputs can be returned in the worker JSON `files` array,
subject to the configured size limit.

For exact video-row tasks, pass `--message-local-id` so AutoPublish refuses to
copy a nearby older cached MP4. If an emoji-heavy group title is hard for OCR,
prefer `expected_title_aliases` such as the title without emoji. The relaxed
`allow_title_guard_fallback` path is for dry-run/review only; live sends still
fail closed unless `allow_live_title_guard_fallback` is deliberately set. Avoid
that live override for multi-chat monitors because it can post into the wrong
visible group.

## External Decrypt Backend

The optional second solution uses `ylytdeng/wechat-decrypt` as a private receive
backend while keeping LabCanvas GUI sending unchanged:

```bash
labcanvas wechat backend install --skip-deps
labcanvas wechat backend status --json
labcanvas wechat backend probe --json
labcanvas wechat backend init-config --json
labcanvas wechat backend decrypt --incremental
labcanvas wechat backend monitor-web --port 5679
labcanvas wechat backend api-history --port 5679 --json
```

Use `find-keys` only when the private key file is missing; Linux key extraction
requires root or `CAP_SYS_PTRACE`. `monitor-web` runs through a LabCanvas
localhost-only launcher instead of exposing the upstream Web UI on all
interfaces. Status output redacts WeChat profile IDs and never prints keys or
decrypted message contents.

## Chat Purpose Modes

Keep one direct config per group. All groups use the shared capability framework
in `scripts/wechat_chat_profiles.py`; a profile changes ordinary behavior and
scheduled output only. Safe explicit requests can use the same research,
artifact, CAD/PCB, Blender, presentation, media, and publication routines in
every group. `LazyResearch`, `🍓My devices`, and WeCom `LabAgent` are the
three reference templates; LabAgent keeps its existing shared-group ban on
public video publication. Exact chats still have separate context, queues,
files, delivery ledgers, and resumable sessions. `LazyResearch` uses the
research profile. Language-learning chats
such as `EchoMind` can use:

```json
{
  "respond_to_all": true,
  "respond_to_self": false,
  "trigger_local_types": [1],
  "chat_purpose": "language_learning",
  "analysis_mode": "echomind_language",
  "immediate_route_enabled": true,
  "immediate_ack_enabled": false,
  "agent_route_enabled": true,
  "agent_route_prefilter": "agent_first",
  "codex": {"model": "gpt-5.5", "reasoning_effort": "medium"}
}
```

EchoMind replies to normal messages with Japanese furigana/romaji, Chinese
pinyin, grammar notes, and English glosses. The direct monitor silently ignores
messages that request secrets, credentials, payment/order actions, destructive
commands, prompt disclosure, or bot rule changes.
EchoMind is language-learning by default, not language-only: explicit
CAD/PCB/image/video/publish/writing/LaTeX/PDF artifact requests route through
the same per-chat route agent and worker routines as other monitored chats.
Use `immediate_ack_enabled: false` only to suppress the visible acknowledgement;
leave `immediate_route_enabled: true` so backend work still enters the queue.
Keep `ignore_self_messages: true` so EchoMind does not analyze or repeat its own
previous output. Enable `respond_to_self` only for short manual tests where
phone-sent messages from the same logged-in account should trigger replies.

The tmux supervisor runs a single decrypt refresh process and launches each
direct group monitor with `--no-decrypt`. This keeps `懒人科研`, `EchoMind`, and
other configured groups independent while avoiding concurrent decrypt stalls.
The refresh process uses `labcanvas wechat backend decrypt --incremental`
through the same backend wrapper as the CLI, and skips decrypt work when the
source DB/WAL timestamp is unchanged. `labcanvas wechat health --json` reports
the external backend state next to per-group catch-up status, monitor freshness,
readiness, chat-idle state, and latest-row age.
Research configs can enable attachment triggers for image/video/file rows, and
long or obviously multi-step research messages route directly to the worker
even without a known keyword. EchoMind keeps attachment triggers disabled so it
only responds to language-learning text.
Bare file uploads route to the source-scoped `file_intake` routine. The worker
copies the exact attachment into `output/wechat_worker/<task-id>/intake/`, then
uses `wechat_document_reader.py` for ZIP, RAR, 7z, Word, PDF, and text files. Readable
documents reach the resumed per-chat agent as an `agent_context_path`, so a
bare upload receives a concise natural identification and preliminary summary
instead of a checksum receipt. Explicit requests can summarize, extract,
compare, or translate the same context. Unsupported, encrypted, oversized, or
missing files still fail closed with a receipt. See
[`docs/WECHAT_DOCUMENT_INTAKE.md`](docs/WECHAT_DOCUMENT_INTAKE.md).
Each group can keep two private Codex sessions, `fast` and `worker`, in
`.private/codex_sessions/`. Session keys include a short hash of the exact chat
title, so non-ASCII groups such as `懒人科研` and `鏈接` cannot collapse into the
same reusable thread. If `labcanvas wechat status --json` reports
`legacy_key: true`, back up and remove that old registry before restarting the
monitors. Set `WECHAT_CODEX_REUSE_SESSIONS=0` to force stateless turns.
Codex is the default agent backend. To run a Windows/WSL deployment with Claude
Code, set `agent_backend: "claude"` in the ignored direct-chat config or export
`WECHAT_AGENT_BACKEND=claude`; the router and worker still use the same
coalescing, safety, queue, and artifact-delivery logic. Claude route/fast turns
use read-only tool blocks, and worker turns remain opt-in. See
[`docs/WSL_WINDOWS_DEPLOY.md`](../../docs/WSL_WINDOWS_DEPLOY.md).
Worker sessions use `gpt-5.5` and `danger-full-access` by default so downloads
and external tooling are not blocked by the shell sandbox; set
`WECHAT_WORKER_CODEX_SANDBOX=workspace` to downgrade for a restricted run.
Set `WECHAT_WORKER_MIN_EFFORT`, `WECHAT_WORKER_MAX_EFFORT`, or
`WECHAT_WORKER_MAX_CODEX_ATTEMPTS` to tune dynamic escalation. Spark worker
models are ignored unless `WECHAT_ALLOW_SPARK_WORKER=1` is set intentionally.
Direct monitors should keep `agent_route_enabled=true` and
`agent_route_prefilter=agent_first` for monitored chats. The
per-chat `route` Codex session classifies route kind, project, source policy,
and worker need before keyword lists; deterministic heuristics remain fallback
and safety logic.
When `dynamic_ack_enabled=true`, that same route decision can provide the
short visible ACK, so task confirmations are contextual instead of mechanical.
The static `immediate_ack_text` remains a fallback when the agent omits an ACK
or produces text that mentions internals.
For login/CAPTCHA/download blocks, open a browser in the same isolated noVNC
desktop with `labcanvas wechat browser-assist --url "<url>" --json`; the user
handles the manual step and the worker continues after approval.
For WeChat official-account links, `mp.weixin.qq.com` often returns `环境异常` or
`完成验证后继续访问` to desktop fetches. Research tasks now run
`wechat_source_recovery.py`: it retries with a mobile WeChat user agent, extracts
`#js_content`, and stores successful Markdown/HTML in ignored private/task
artifacts. If extraction still fails, the worker receives exact title/account
and identity queries for public-source reconstruction. Read-only recovery never
opens/focuses a browser and never asks the user to verify; it returns a concise
evidence-limited answer when neither extraction nor reconstruction succeeds.
Private send targets should include `expected_title`; before composing, the GUI
sender OCR-checks the opened chat header and fails closed if the wrong group is
visible. All GUI sends use `.private/wechat_gui_send.lock`; do not run parallel
raw click/paste senders against the same WeChat desktop. Do not use WeChat
search as the normal send path; the sender uses the current verified chat plus
configured `open_click`/`fallback_clicks` and otherwise fails closed. Use the
controlled browser/browser-assist workflow for web/source searches. If OCR
repeatedly misreads a group title, add `expected_title_aliases` for the observed
OCR text.

## Group Creation

Group creation is intentionally gated. Prefer search-based selection by contact
alias/name, then set group settings with the guarded admin helper:

```bash
python3 agentic_tools/wechat_gui_agent/scripts/wechat_group_create.py \
  --display :97 \
  --member-query lachlach \
  --member-query lachlanchen \
  --member-query lachlanchan
```

Only pass `--create` after the selected members are verified, because WeChat
notifies real accounts when a group is created:

```bash
labcanvas wechat create-group \
  --member-query lachlach \
  --member-query lachlanchen \
  --member-query lachlanchan \
  --create
```

Set the group name and this account's in-group alias through Settings:

```bash
labcanvas wechat rename --chat "EchoMind" --name "EchoMind"
labcanvas wechat alias --chat "EchoMind" --name "LazyingArt"
labcanvas wechat alias --chat "懒人科研" --name "LazyingArt"
```

The group admin helper edits the `Group Name` or `My Alias in Group` row,
captures screenshots, OCR-checks that the target row contains the requested
text, then clicks WeChat's `Modify` confirmation. Keep the OCR guard enabled
unless a human is watching noVNC.

See `docs/GITHUB_OPTIONS.md` for the GitHub automation options checked before
choosing the visible Linux GUI route.
See `docs/RUNBOOK.md` for the repeatable operator workflow.

## Guardrails

- It does not bypass WeChat login; approve the desktop login from the phone first.
- It sends only when `--send` is supplied.
- It targets the visible WeChat desktop, so keep noVNC open for human inspection.
- It stores private target files and mirror data under `.private/`, which is ignored.
- It is intended for small, explicit sends such as test messages, not bulk spam.
- Each monitored group needs its own `message_table`, `state_path`, and
  `send_target` so replies return to the correct chat.
