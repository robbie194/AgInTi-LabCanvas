# Repository Guidelines

## Project Structure & Module Organization

AgInTi LabCanvas is a small Python CLI and web package. Production code lives in `src/agenticapp/`: `cli.py` handles commands, `workspace_agent.py` runs persistent tool-capable chat sessions and durable tasks, `knowledge/workspace_agent.md` packages cross-tool engineering knowledge, `config.py` loads target registries, `adapters.py` dispatches instructions, `webapp.py` serves the local studio, and `artifacts.py` tracks generated files. Tests live in `tests/`. Static web assets live in `src/agenticapp/web/static/`.

## Build, Test, and Development Commands

- `PYTHONPATH=src python -m agenticapp list`: list configured Blender, BioRender, Unity, and Unreal targets.
- `labcanvas list`: run the installed console command.
- `PYTHONPATH=src python -m agenticapp doctor`: validate target configuration without sending commands.
- `PYTHONPATH=src python -m agenticapp dispatch blender "Create a cube" --dry-run`: inspect the JSON envelope for a target.
- `PYTHONPATH=src python -m agenticapp mcp-config`: emit MCP client configuration.
- `PYTHONPATH=src python -m agenticapp scene-template experiment-setup`: print a reusable 3D experiment scene spec.
- `PYTHONPATH=src python -m agenticapp render-scene examples/paper-optics-setup.scene.json --dry-run`: validate a scene spec and output paths.
- `PYTHONPATH=src python -m agenticapp web --port 8787`: start the local chat, canvas, and preview web app.
- `PYTHONPATH=src python -m agenticapp webapp start --port 19473`: start the studio in tmux.
- `PYTHONPATH=src python -m agenticapp agent capabilities`: inspect integrated CAD, KiCad, Blender, TeX, WeChat, LabVIEW, figure, and bridge readiness.
- `PYTHONPATH=src python -m agenticapp agent chat "Design and render a C-mount holder"`: run a persistent direct agent turn with dynamic model/effort selection.
- `PYTHONPATH=src python -m agenticapp agent chat "Rebuild the exact Shapr part" --model gpt-5.6-sol --effort medium`: explicitly use GPT-5.6 SOL with medium reasoning.
- `PYTHONPATH=src python -m agenticapp agent tasks`: list durable agent work and artifact status.
- `PYTHONPATH=src python -m agenticapp grant init "Draft specific aims" --title "Research Grant"`: create an ignored evidence, figure, LaTeX, PDF, and goal workspace.
- `PYTHONPATH=src python -m agenticapp grant run "Complete the proposal" --project-dir output/grants/PROJECT --effort medium`: resume a persistent GPT-5.6 SOL grant agent against one durable workspace.
- `PYTHONPATH=src python -m agenticapp grant compile --project-dir output/grants/PROJECT && PYTHONPATH=src python -m agenticapp grant validate --project-dir output/grants/PROJECT`: compile and enforce evidence, editability, PDF, and artifact gates.
- `PYTHONPATH=src python -m agenticapp presentation init "Research roadmap" --objective "Explain the evidence and next experiment" --output-dir output/presentations/roadmap`: create an editable manifest-driven deck workspace.
- `PYTHONPATH=src python -m agenticapp presentation build output/presentations/roadmap/presentation.json --render --json`: build and validate the PPTX, then render PDF/PNG previews.
- `PYTHONPATH=src python -m agenticapp presentation validate output/presentations/roadmap/presentation.json --json`: enforce editable text, bounded generated assets, prompt provenance, and generated-text review gates.
- `agentic_tools/biorender_agent/scripts/start_biorender_stack.sh`: reuse the dedicated BioRender browser and authenticated localhost MCP proxy.
- `python agentic_tools/biorender_agent/scripts/probe_biorender_mcp.py --json`: prove authenticated MCP `initialize` and `tools/list`, rather than relying on an HTTP health check.
- `PYTHONPATH=src python -m agenticapp studio biorender-figure "Mechanism figure" --panel "A: mechanism" --panel "B: validation" --json`: create a reviewable dry-run contract; add `--live` only when an editable BioRender figure should actually be created.
- `PYTHONPATH=src python -m agenticapp protein start --json`: start the persistent AlphaFold Server Chrome/CDP/noVNC stack while reusing the existing login profile.
- `PYTHONPATH=src python -m agenticapp protein status --json`: verify the browser, CDP page, noVNC endpoint, profile, submodule, and sibling artifact workspace.
- `PYTHONPATH=src python -m agenticapp protein submit references/target.fasta --dry-run`: validate a protein job through the existing `ProteinStructure` submitter without consuming an AlphaFold Server job.
- `PYTHONPATH=src python -m agenticapp protein poll --download --all-pages`: use the existing browser workflow to poll and download completed results.
- `PYTHONPATH=src python -m agenticapp protein metrics --detailed && PYTHONPATH=src python -m agenticapp protein render all`: extract metrics and render local plots/backbones from downloaded outputs.
- `PYTHONPATH=src python -m agenticapp social project add --repo ../ZhJpBook --id pocketpolyglot`: register repository evidence for social campaigns.
- `PYTHONPATH=src python -m agenticapp social campaign create --project pocketpolyglot --name introduction --objective "Introduce the usable open-source Studio" --platform x --platform reddit:r/languagelearning --platform hackernews`: create a platform-specific campaign brief.
- `PYTHONPATH=src python -m agenticapp social draft generate CAMPAIGN_ID --dry-run --json`: inspect the medium-effort Codex drafting contract without quota use or external writes.
- `PYTHONPATH=src python -m agenticapp social maintain CAMPAIGN_ID --integration x=POSTIZ_X_ID --days 30 --dry-run`: inspect the source-grounded analytics and follow-up contract without provider reads or model quota.
- `PYTHONPATH=src python -m agenticapp books status --json`: inspect the sibling Books guarded-browser search and PocketPolyglot Studio control planes.
- `PYTHONPATH=src python -m agenticapp books search "Pride and Prejudice" --language eng --title-term "Pride and Prejudice" --json`: return metadata-ranked book/detail candidates without authorizing or performing a copyrighted download.
- `PYTHONPATH=src python -m agenticapp books polyglot projects --json && PYTHONPATH=src python -m agenticapp books polyglot progress --project PROJECT --json`: inspect durable multilingual-book projects and browser-visible progress through the existing `../ZhJpBook` Studio.
- `PYTHONPATH=src python -m agenticapp feedback targets --json`: list allowlisted sibling repositories for evidence-based local bug, feature, and handoff reports.
- `PYTHONPATH=src python -m agenticapp feedback write lazyedit feature "Expose a job-scoped QR artifact" --verified --json`: create or revise an idempotent privacy-sanitized report without authorizing a public issue, commit, or push.
- `PYTHONPATH=src python -m agenticapp studio figure-grid "optical icons 2x3" --rows 2 --cols 3`: run the same artifact action as the web canvas.
- `PYTHONPATH=src python -m agenticapp studio dispatch blender "Prepare an editable paper figure setup"`: dry-run a configured target and register the envelope as an artifact.
- `PYTHONPATH=src python -m agenticapp wechat worker --chat "懒人科研" enqueue "Use LabCanvas to render a PCB and CAD preview"`: enqueue slower WeChat backend work that can call CAD, PCB, Blender, and LabCanvas tools.
- `PYTHONPATH=src python -m agenticapp wechat worker reprocess TASK_ID "recover completed report" --artifact-recovery-only --send --queue agentic_tools/wecom_agent/.private/wecom_task_queue.jsonl`: compile and deliver a completed exact-task research report without rerunning the agent.
- `PYTHONPATH=src python -m agenticapp wechat worker repair-result TASK_ID --send`: reapply the current safety/delivery contract to one stored raw agent result and deliver it without rerunning the model or task tools.
- `PYTHONPATH=src python -m agenticapp wechat init-config --agent-backend claude --force`: write ignored WeChat config templates that opt into Claude Code; Codex remains the default backend.
- `PYTHONPATH=src python -m agenticapp wechat selftest --suite all --json`: prove WeChat transport, routine contracts, Codex resume, and publish poststage repair work together.
- `PYTHONPATH=src python -m agenticapp wechat selftest --suite publish-poststage --json`: prove the WeChat worker can repair missing LazyEdit publish jobs, avoid duplicates, and pause on login blockers.
- `PYTHONPATH=src python -m agenticapp wecom init-config && PYTHONPATH=src python -m agenticapp wecom install`: prepare the ignored official WeCom AI Bot WebSocket sidecar.
- `PYTHONPATH=src python -m agenticapp wecom gateway start`: run the WeCom gateway and its transport-aware LabCanvas worker in tmux.
- `agentic_tools/wecom_agent/scripts/install_wecom_autostart.sh install`: enable the lingering user service that restores the full WeCom tmux, GUI/noVNC, worker, scheduler, and knowledge stack after reboot and repairs missing windows without switching accounts.
- `PYTHONPATH=src python -m agenticapp wecom gui init --chat LabAgent && PYTHONPATH=src python -m agenticapp wecom gui restart --json`: start the exact-name external-group GUI relay when the tenant blocks official external-group APIs.
- `PYTHONPATH=src python -m agenticapp wecom gui init --chat AgentTest --allow-search-fallback && PYTHONPATH=src python -m agenticapp wecom gui guide --chat AgentTest --live --json`: add a second exact group and send its idempotent task guide.
- `PYTHONPATH=src python -m agenticapp wecom gui messages --chat LabAgent --after 0 --limit 100 --json`: consume normalized inbound messages through a durable cursor.
- `PYTHONPATH=src python -m agenticapp wecom gui send --chat LabAgent --file output/report.pdf --task-id report-1 --live --json`: deliver a repository artifact with idempotency and composer/history verification.
- `PYTHONPATH=src python -m agenticapp wecom android init --serial SERIAL --chat LabAgent --chat AgentTest --force --json`: configure the ignored allowlisted MIX 2S WeCom transport.
- `PYTHONPATH=src python -m agenticapp wecom android send --chat LabAgent --mention '<SENDER_DISPLAY_NAME>' --message '处理完成。' --task-id TASK_ID --live --json`: send through the native phone client with an exact rich member mention.
- `PYTHONPATH=src python -m agenticapp wecom external init --chat AgentTest && PYTHONPATH=src python -m agenticapp wecom external authorize --json`: configure the separate official external-group CLI transport and keep its QR/bridge guard alive.
- `PYTHONPATH=src python -m agenticapp wecom client start --json`: open the optional official WeCom Windows client in an ignored Wine prefix and dedicated localhost noVNC desktop for enrollment only.
- `PYTHONPATH=src python -m agenticapp wecom daily status --json`: inspect enrolled WeCom research groups and their `#daily` topics without exposing raw chat IDs.
- `PYTHONPATH=src python -m agenticapp wecom daily run --force --json`: enqueue today's idempotent research briefing immediately for enrolled groups.
- `PYTHONPATH=src python -m agenticapp wecom knowledge status --json`: inspect private per-member event, paper, file, and memory counts without raw transport IDs.
- `PYTHONPATH=src python -m agenticapp wecom knowledge search --member-key MEMBER_KEY --kind insight --query mechanics --json`: query one member's private, provenance-aware knowledge partition.
- `python3 agentic_tools/wechat_gui_agent/scripts/codex_quota_status.py probe --json`: read and privately cache the normal Codex rolling quota through the official local app-server API without spending model tokens.
- `PYTHONPATH=src python -m agenticapp wechat audio-intake --input <media> --output-dir <task-dir> --json`: transcribe one exact local WeChat audio/video attachment into reusable worker context.
- `PYTHONPATH=src python -m agenticapp wechat native-save-video --target <ALLOWLISTED_TARGET> --task-id <TASK_ID> --output-dir <TASK_DIR>/native_original --filename <NAME>.mp4 --video-tap x,y --json`: save one exact Android WeChat video through `查看原视频` and the native album action, verify the host file, then remove the temporary phone export.
- `PYTHONPATH=src python -m agenticapp wechat shipinhao-transcribe --source-text-file <card.txt> --output-dir <task-dir> --json`: run exact-card URL, public-excerpt, cache, and Whisper recovery directly.
- `PYTHONPATH=src python -m unittest discover -s tests`: run the full test suite.
- `scripts/mix2s on|off|status [--serial SERIAL]`: start, completely stop/sleep, or inspect the isolated MIX 2S scrcpy/noVNC desktop without affecting WeChat or WeCom.
- `scripts/install_blender_portable.sh`: install a no-sudo Blender binary under `~/.local/share/labcanvas/blender`.
- `labcanvas --config configs/blender-local-command.example.json dispatch blender "Draw a building"`: run the local Blender bridge.
- `agentic_tools/labview_mcp_agent/scripts/probe_labview.sh`: check local LabVIEW/NI package state.
- `agentic_tools/labview_mcp_agent/scripts/install_mcp_candidate.sh nineman`: clone/update the recommended LabVIEW-hosted MCP toolkit outside git.
- `agentic_tools/labview_mcp_agent/scripts/install_labview_linux.sh --dry-run`: verify NI installer availability before attempting a proprietary LabVIEW install.
- `agentic_tools/jlcpcb_order_agent/scripts/jlc_browser_stack.sh config --json`: inspect the dedicated JLC display/noVNC/CDP/profile contract without starting or changing any browser.
- `agentic_tools/jlcpcb_order_agent/scripts/jlc_browser_stack.sh start`: start or reuse only the dedicated JLC browser stack on display `:104`, noVNC `6124`, and CDP `49237`.

## Coding Style & Naming Conventions

Use Python 3.10+ and the standard library unless a dependency clearly improves the project. Follow PEP 8 with 4-space indentation. Use `snake_case` for modules, functions, and variables; use `PascalCase` for dataclasses and exceptions. Keep CLI output stable because tests and downstream scripts may parse it.

## Testing Guidelines

Use `unittest` for now. Name test files `test_*.py` and keep tests focused on behavior: config validation, dispatch envelope shape, transport behavior, and CLI return codes. Add regression tests when changing adapter semantics or target config parsing.
For scene rendering, test JSON validation and dry-run plans without requiring Blender; use a manual render check when changing `src/agenticapp/blender/scene_renderer.py`.
For web changes, keep tests focused on API behavior, artifact registration, and static startup; manually verify the browser layout with the local server.
For workspace-agent changes, mock the backend in unit tests. Test model selection, prompt contracts, task transitions, session isolation, cancellation, and artifact registration without spending live model quota; then run one small live smoke task when backend invocation changes.

## Workspace Agent Rules

The web and CLI must use the same `workspace_agent.py` runtime. Keep the web chat a direct transport to a persistent agent session; do not replace it with keyword-specific response branches. Domain-specific code remains useful as callable routines. Automatic routing reads `configs/model-policy.json`: Codex (`auto-code-review` or `gpt-5.6-sol`) is the primary backend. An unavailable Codex turn falls back to AgInTiFlow, where DeepSeek is the normal provider and LocalLLM is the final same-session capability fallback. Claude Code remains an explicit opt-in compatibility backend. Explicit backend/model/effort choices remain authoritative. Preserve durable task records under ignored `output/`, return real artifacts to the canvas, and require explicit authorization for payment, manufacturing submission, public publication, credential changes, destructive deletion, or another irreversible external action.

Keep JLC web ordering isolated from the existing AgInTi Browser/Xiaoyunque
identity. JLC owns display `:104`, VNC `5924`, noVNC `6124`, CDP `49237`, and
profile `~/.cache/jlcpcb-order-shared`. Reuse that stack and its existing tab;
never use `JLCPCB_TAB_CDP_PORT` to inject JLC into AgInTi Browser CDP `9344`.
Do not stop or reopen AgInTi Browser/XYQ while preparing a JLC order. The
Books/LibGen browser remains independently owned by the Books project.

## Presentation Rules

Presentation requests use `src/agenticapp/presentations.py` and the
`presentation_deck` worker routine. Keep native editable text, slide geometry,
and a `presentation.json` manifest as the source of truth. Image generation is
allowed only for separate bounded material assets such as illustrations,
photos, icons, textures, or panels; never generate a complete slide or slide
background as one image. Preserve the prompt/provenance for each generated
asset. Generated text is allowed only when it has a transcript and explicit
review, while every essential title, claim, citation, label, and body sentence
remains editable slide text.

When style is unspecified, start immediately with the bright scientific theme
and tell the requester they may add audience, color, style, logo, or content
preferences while work continues. Ask only one concise question when a missing
fact materially affects the deck and cannot be inferred. Treat subsequent
same-chat messages as interruptions to the persistent presentation session.
Substantive deck research and synthesis may use GPT-5.6 SOL xhigh; narrow edits
and exports can use lower effort. Validate the PPTX package and render/inspect
PDF/PNG previews before delivery.

Protein structure tasks must reuse `external/ProteinStructure` and its sibling runtime workspace `../ProteinStructure`; do not rewrite AlphaFold submission, polling, metric extraction, plotting, or screenshot logic in LabCanvas. Keep generated AlphaFold downloads, figures, compiled PDFs, copied result payloads, screenshots, and runtime logs local and ignored. Track reusable scripts, runbooks, FASTA/JSON job inputs, compact metrics, TeX/Markdown sources, and reference structures. Route nontrivial AlphaFold/protein-structure work to `gpt-5.6-sol` with medium effort, preserve the logged-in CDP profile, and distinguish prediction, literature evidence, docking hypotheses, and experimentally validated inhibition.

## Figure Pipeline Rules

Paper figure generation must stay editable and atomic. Do not treat a generated bitmap as the final source of truth. Use image generation for overview concepts, then split figures into named parts with their own prompts, source files, tool settings, previews, and edit history. Prefer BioRender for academic assets, OpenSCAD for device geometry, Blender for rendered setups, LabVIEW for instrument/control workflows, and TeX for clipping and final assembly. Preserve part IDs and rebuild exports from manifests.

## Grant Project Rules

Grant requests use `src/agenticapp/grants.py` and the `grant_proposal` worker routine. Keep one ignored project directory per grant with `goal.json`, `current_request.md`, traceable source metadata, proposal Markdown/LaTeX/PDF, and atomic editable figure sources. Use the Codex `create_goal` tool when the active surface exposes it; otherwise follow the durable `goal.json` contract honestly. Never call `update_goal` or mark the chat task complete until `labcanvas grant validate` passes and `proposal.pdf` is delivered to the exact source chat. BioRender is preferred for suitable authenticated academic assets, but an editable SVG/TeX fallback must keep work moving. Drafting never authorizes submission, payment, credential changes, or invented pilot data, citations, collaborators, facilities, eligibility, deadlines, approvals, or budgets.

BioRender OAuth client data and tokens belong only in `agentic_tools/biorender_agent/.private/` with mode `0600`. Start the dedicated profile/proxy with `start_biorender_stack.sh`; readiness requires the authenticated MCP protocol probe or a visibly logged-in gallery/canvas. Do not depend on dynamic client registration when the upstream edge blocks it, and never print or commit OAuth material.

## Social Content Rules

Use `labcanvas social` for open-source project campaigns. Keep one persistent Codex session per project and store local campaign state under ignored `output/social/`. Ground drafts in registered repository evidence and adapt them independently for each platform; do not copy one advertisement everywhere or invent users, benchmarks, endorsements, or traction. Reddit requires a fresh review of the exact community rules. Hacker News is manual-only because its guidelines reject generated or AI-edited submission text: the agent may prepare verified facts and a human-author worksheet, but not final HN copy. Every external provider write requires both `--live` and a non-expired approval token bound to the exact title, body, target, media, settings, and publication metadata hash. Keep OAuth tokens in the provider-managed user store (Postiz uses `~/.postiz/credentials.json`) and API keys in `agentic_tools/social_content_agent/.private/` or environment variables; never put credentials in campaign exports or git.

## CAD Artifact Sync

For CAD designs, "Nutstore sync" means `/home/lachlan/Nutstore Files/Projects/LabCanvas`. After generating or revising a serious CAD design, copy the final `*_assembly.step` or `*_assembled.step` file to that folder by default, keeping the filename descriptive. Continue preserving the full source/artifact set in the design folder; the Nutstore copy is for Shapr3D/LabCanvas handoff.
Keep each CAD project in one design folder. For major regeneration runs, archive the previous root files under `runs/run-N-human-readable-info-YYYYMMDDTHHMMSSZ/` and keep the root `artifacts/` as the latest checked output. Do not create a sibling design folder for a small geometry fix unless the user explicitly asks for a separate variant.
When a CAD run is ready for 3D printing, create a timestamped run folder under the design, named like `runs/run-N-short-name-print-ready-YYYYMMDDTHHMMSSZ/`; do not leave print-ready files only in the root `artifacts/` folder. Also create a clean Nutstore subfolder named like `<design>/run-N-short-name-print-ready-YYYYMMDDTHHMMSSZ/`. Include `PRINT_THIS_*.stl`, `PRINT_THIS_*.step`, `PRINT_THIS_*.3mf`, separate part STEP files such as `bottom_part.step` and `top_part_180deg_print.step` when relevant, a manifest, README, and render PNGs for both the final single assembly and the exact direct-print layout. For large flat parts, default to removable anti-warp ears with side pulls plus diagonal full-corner pull tabs. After the 2026-07 dock print feedback, make these ears stronger/larger by default: use roughly 0.8-1.0 mm sacrificial thickness and larger tail pads unless the user explicitly prioritizes very easy removal.

## WeChat Worker Tool Routing

Immediate execution and delivery contract: creating, enabling, changing, or restarting any routine must invoke it once immediately before waiting for its periodic schedule. A request that asks for a chat response or artifact is incomplete until the sender reports verified delivery, deferred delivery with a durable retry record, or an explicit blocker. Internal-only routines may retain their result locally, but they must state that no chat delivery was requested. This rule applies equally to WeChat and WeCom and to every group, member schedule, language lesson, research task, CAD/PCB task, media task, and publication routine.

Research chat messages that mention LabCanvas, presentations/PPTX, AgInTi image generation, KiCad, Gerber, STEP/STL, CAD, PCB, Blender, figures, icons, or renders should be routed to the worker queue. The fast monitor should only ACK and enqueue. The worker may run `presentation build`, `studio figure-grid`, `studio lab-task`, `render-scene`, AgInTi image generation, KiCad, OpenSCAD, and Blender commands, then return generated PPTX/PNG/PDF/SVG/MP4/MOV/audio/STEP/STL/ZIP/KiCad artifacts in the `files` array so the GUI sender can deliver them to WeChat.
ZIP, RAR, 7z, Word, PDF, and text attachments must pass through `wechat_document_reader.py` after exact same-chat source resolution. Treat the attachment title, extension, size, and checksum as an identity contract; never substitute nearby media. Parse DOCX as XML, extract PDFs with bounded `pdftotext` plus OCR fallback, and unpack archives only with traversal, symlink, encryption, member-count, byte, depth, executable, and compression-ratio guards. Pass `agent_context_path` to the resumed worker so it answers from the content; do not stop at a checksum receipt when readable evidence exists.
For incoming images, use exact same-chat media resolution and Codex vision to answer naturally with what the image shows or means. On WeCom Android, request `查看原图`, save through the native viewer, and verify the Android MediaStore export before analysis; never pass a chat-bubble crop, viewer screenshot, or compressed preview to vision/OCR as the source image. For image/media analysis, always provide useful Chinese, English, and Japanese detail; add pinyin, Japanese kana/furigana, romaji, pronunciation, grammar, or vocabulary when text or language-learning content is present. Keep OCR, model names, dimensions, checksums, and fixed vision labels as private evidence; expose them only when the user explicitly asks for transcription or diagnostics.
Treat file modification time as discovery evidence only. Exact-task media sync must match a same-message token such as local/server ID, attachment title, checksum-derived name, or native media token; production workers must reject mirror rows recorded only as `matched_by=mtime`, even if a background cache scan copied the same recent file into multiple private chat folders.
EchoMind has exactly two scheduled outputs: one compact multilingual teaching message every six hours, and one high-quality previous-day language review PDF at 06:00 `Asia/Hong_Kong`. Each periodic message must be concise but complete: align Chinese, English, and Japanese examples, include full tone-marked pinyin, use WeChat-safe inline Japanese ruby/furigana such as `予約（よやく）`, and include romaji. Never truncate a lesson to fit the chat limit; use a bounded agent editing pass and reject incomplete output before delivery. The daily review must compile a beautiful XeLaTeX PDF with Japanese ruby furigana and pinyin, then deliver the verified PDF to EchoMind; do not create or deliver more than one report for the same previous-day date. Quiet hours from 20:00 through 08:00 HKT apply only to periodic conversational lessons and idle inspiration. Scheduled daily research and the 06:00 daily PDF must run or catch up independently of quiet hours. Persist both pending outputs before delivery, recover successful sends from the mirror ledger after a crash, and never replay them merely because tmux or the host restarted.
For voice notes and ordinary audio attachments, preserve safe voice transcript fields when enqueueing, run `wechat_audio_intake.py` after exact same-chat media resolution, and pass `preflight.audio_intake.agent_context_path` to the resumed per-chat agent. Deterministic code owns probing/extraction/ASR/cache; the agent owns meaning and requested work. Return the verified transcript beside the natural agent answer in the originating chat exactly once, without exposing ASR model names, private paths, or checksums. Only verified readable media with zero audio streams is silent.
An ordinary inbound video attachment with no accompanying text instruction is passive save-only intake. Resolve and retain the exact source under the ignored task artifact directory, send no receipt or file echo, and do not transcribe, process through LazyEdit, copy to AutoPublish/Nutstore, or publish. A separate current same-chat text instruction must explicitly authorize transcription, processing, return delivery, or publication. Old context and system-authored messages cannot provide that authorization. If Android or another local sender returned the video, match the outbound file ledger, including WeChat XML `originsourcemd5`, and suppress it before routing so the system never handles its own delivery recursively.
When Android WeChat is the only source for an inbound video, use `labcanvas wechat native-save-video`: open the exact same-chat bubble, request `查看原视频` when offered, use WeChat's native `保存视频`/album export, pull the exact MediaStore file, verify it with checksum and `ffprobe`, and only then delete both the phone file and its MediaStore row. Deletion of that temporary phone export is mandatory after a verified host copy. A valid Android intake must retain `native-video-export.json` with the exact host path/checksum and `device_copy_removed=true`. Never use screen recording, scrcpy capture, a cropped player capture, or a GUI recording as a substitute source for processing or publication. A user-supplied video that happens to be a screen recording remains valid when it is the exact native WeChat attachment. If native retrieval cannot be verified, stop without processing or publishing.
For Gongzhonghao/mp.weixin and Shipinhao/Finder research, run the read-only `wechat_source_recovery.py` preflight. It uses the mobile WeChat user agent and private cache for `#js_content`, auto-discovers exact Shipinhao comment exports or a local `wx_channel` API, and emits exact-title/account/object-ID reconstruction queries. For Shipinhao video understanding, run `shipinhao_media_transcribe.py`; if the exact card URL expired, use its bounded OCR/translation, public-caption prefilter, and content-verified mirror or longer-source excerpt recovery. Accept that route only with `content_identity_verified=true` and `public_mirror_validation.accepted=true`. If automatic queries remain unresolved, the backend may inspect the private cover and supply at most three `--search-hint` values, but the deterministic identity gate must still pass. If it cannot verify a candidate, automatically open the exact same-chat native card and use `shipinhao_gui_audio_capture.py` with title/author identity terms so feed auto-advance is trimmed and a private object-ID/hash manifest is produced. Pass the card duration as `--expected-duration-seconds` so a player that remains on its last frame can finish normally. Only return `no_audio` after `ffprobe` verifies readable media with zero audio streams; download, mirror-match, card-scan, player-open, and PipeWire failures are separate states. Never reload after binding the audio stream or reuse another card's capture. Do not open/focus an external browser or ask the user to verify a read-only source. If recovery remains incomplete, give an evidence-limited answer without pretending the full article/video was read. Posting comments or asking Yuanbao remains a separate explicit write action.
Before resuming the backend, build a bounded task packet containing the current request, exact source IDs, recent same-chat context, interruptions, routine state, and readable context paths. Keep raw Finder XML, signed URLs, secrets, hashes, and unused media paths out of the model prompt.
LALACHAN/Xiaoyunque story and video generation should follow `references/lalachan-story-video-handoff-for-wechat.md`: write the story and prompt first, upload actual reference image files in the documented order, monitor the browser generation to a verified MP4, run the audio/transcript/caption stage for any video with a readable audio stream, and create a source-scoped language-teaching pack with detailed Chinese, English, and Japanese material. Include the original/corrected transcript, translations, pinyin, Japanese kana/furigana and romaji, pronunciation guidance, grammar, and useful vocabulary. Send the MP4 and safe teaching/transcript artifacts back to the originating chat, and only copy to Nutstore or enter LazyEdit/public publishing when the current request asks for those stages.
Video publishing requests should use `agentic_tools/wechat_gui_agent/skills/lazyedit-publish-workflow/SKILL.md`: resolve exact same-chat video media with `labcanvas wechat autopublish-video`, process/publish through LazyEdit's `scripts/lazyedit_publish.py`, monitor local and remote queues, and stop for human QR/CAPTCHA/login steps. LazyEdit must perform transcription, timestamped captions/subtitles, correction, translation, logo/subtitle burn, and metadata processing whenever the source has audio; the worker must additionally produce the detailed Chinese/English/Japanese teaching pack from the verified transcript. A verified silent source may skip subtitle burn and language teaching, but must still be recorded as silent. Preserve the worker's video publish/subtitle context bundle as `--correction-prompt-file`, create a separate concise `--metadata-prompt-file`, and only return safe source-scoped media artifacts.
The tmux supervisor must launch `wechat_worker_guarded_loop.sh`, not the raw worker, so the publish-poststage self-test runs before the worker loop starts.
Personal-WeChat recovery after restart must be recent and bounded. It may resend only stored results for exact personal transport rows within the configured age/limit, must not rerun model work, and must never drain an old queue backlog. Keep file selection and submission inside one exact-chat GUI lock transaction and verify the target header both before and after the native picker.
Never truncate a worker or fast-agent answer before delivery. Send moderate answers as coherent numbered, retry-safe parts; when the complete response would exceed three normal chat parts, compile an exact-task `complete-response.pdf`, deliver it once with a concise preview, and keep its Markdown source local. If compilation fails, send all numbered parts. Treat explicit backend truncation markers as incomplete output and repair them before delivery.
Treat WeChat as message transport only: nontrivial messages should become queued tasks with `task.routine` and `execution_contract`, then `wechat_task_worker.run_task_orchestrator` resumes the exact chat's selected backend session through `wechat_agent_backend.run_agent_session`. The automatic backend order is Codex first, then AgInTi. AgInTi uses DeepSeek as its normal provider and hands the same durable session to LocalLLM only as its final capability fallback. Explicit backend choices remain authoritative. Backend-unavailable and agent-turn timeout failures should degrade through the centralized fallback policy rather than leaving a group silent or replaying completed side effects.
When an exact current message explicitly requests a bug report, feature request, or integration handoff for an allowlisted sibling project, route it to `cross_repo_feedback`. The worker agent must reproduce/inspect the gap or state a concrete current feature requirement and return structured `upstream_feedback`; deterministic code may only validate, redact, and idempotently write it with `labcanvas feedback`. Reject transient login/CAPTCHA/quota/network/timeout/transport failures, keep the report local unless the user explicitly asks for the file, and never treat a local report as permission for a public issue, commit, push, or release.
For the `鏈接` read-later group, default to one concise source-grounded chat summary and keep Markdown, TeX, screenshots, and intermediate reports local. Send a compiled PDF only when the current message explicitly requests a report/PDF; send Markdown or TeX only when source files are explicitly requested. The daily `写作 外语 挣钱` organizer uses its own exact-chat `daily_organizer` session, deduplicates memory rows, compiles one Chinese XeLaTeX PDF, and delivers only that PDF. Persist generation separately from delivery so a transient GUI failure retries the existing PDF without another agent turn or duplicate chat output.
For proactive quota visibility, use `codex_quota_status.py` and the WeCom tmux `quota` window. Read only `account/rateLimits/read`, select the normal `codex` bucket rather than independent model buckets, and keep percentage/reset metadata in ignored private storage. When remaining quota is strictly below 5%, prepend one concise warning to each actionable WeCom direct reply or queue acknowledgement without blocking the task; do not warn again in the final worker result, on duplicate rows, or during silent peer conversation.
Official WeCom bot DMs/groups use `agentic_tools/wecom_agent/` as an alternate transport into the same worker runtime. Keep BotID, Secret, raw user/chat IDs, media, message history, and delivery ledgers in ignored private/output paths. The localhost send API must authenticate, reject unseen chat IDs, and preserve per-chat session isolation. Do not claim this API can read arbitrary existing personal-WeChat groups.
For the external-group GUI relay, QR-login and explicit security-verification screens always fail closed. A persistent `device_environment_abnormal` composer warning may use the configurable verified file-only fallback: files still require exact native-picker identity and a newly visible history card, while text and inbound polling remain quarantined.
Maintain WeCom member knowledge in the ignored `wecom_member_knowledge.sqlite` database and `output/wecom/member_knowledge/` archive. Partition ownership by stable hashed member key and exact chat; record source message/task provenance and checksums for files. Ingest exact inbound attachments immediately, index completed worker/daily artifacts asynchronously, and pass only a bounded same-member/same-chat memory view to agents. Never merge members by similar display names or expose raw IDs, database internals, credentials, hashes, or another member's records. Keep the `knowledge` tmux indexer incremental and quota-free while idle.
For the allowlisted WeCom GUI fallback, native per-bubble Copy and Wine Unicode clipboard text are authoritative; OCR is only a locator/fallback. Preserve exact copied text in `request` and `original_request`, force the viewport to the live tail before polling, and treat route plans as advisory. Resolve scientific letter/digit ambiguities with authoritative web evidence before asking for clarification.
Keep WeCom and personal-WeChat ingress separate. Internal bot chats use `wecom_bot_websocket`; an explicitly authorized external WeCom group may use `wecom_cli`, isolated `wecom_gui`, or allowlisted `wecom_android` when the tenant does not grant CLI message access. Carry `wecom_transport_channel` through ingress, durable tasks, daily scheduling, and delivery; never fall back from any WeCom channel to the personal-WeChat GUI/database or sender. Match external groups by one exact configured name, fail closed on duplicates, and seed old history instead of replaying a restart backlog.
The external authorization guard must use only the dedicated WeCom admin browser/profile and ignored `wecom-cli-message-config` state. Restart only its `external` tmux window during authorization; do not interrupt the internal bot gateway. The isolated Wine desktop client is an optional fallback. The owner-authorized Android client may be an active WeCom-only ingress/delivery transport, but must use exact allowlisted groups, localhost authentication, title verification, stable task IDs, and a normal unlocked keyguard; it never inherits personal-WeChat state. For group replies, preserve the exact current sender display name and let the Android bridge select one native mention-picker row. Accept only WeCom's deterministic `@微信` row suffix, reject ambiguity/broadcasts, and never emulate a mention with plain `@name` text. Start WeCom work through `wecom_worker_loop.sh`, which disables personal-WeChat GUI/media/Android and publication fallbacks before invoking the shared LabCanvas routine orchestrator. Reusing execution routines is allowed; reusing personal-WeChat source resolution, media lookup, mirror database, or delivery for a WeCom task is not. Keep WeCom telemetry under ignored `output/wecom/`.
The paired owner may enroll a WeCom group; trusted members of that exact group may request research, paper/PDF, figure, CAD/PCB, Blender, and design artifact work with the same worker permissions as the private LazyResearch group. LabAgent does not perform video publication or other public posting. Soft-filter dangerous or out-of-scope requests through the route agent, while retaining hard exact-chat/source isolation and existing approval gates for sensitive actions. `#daily <topic>` stores a private per-member research preference and immediately queues one idempotent first briefing in addition to future scheduled reports. Daily tasks have no queue deadline, use dynamic GPT-5.6 SOL effort, and return Markdown plus a LaTeX-compiled Nature-style PDF. If the backend stops after writing a complete exact-task report, recover and deliver those artifacts without rerunning the research or replaying the acknowledgement.
When changing WeChat automation behavior, also update `agentic_tools/wechat_gui_agent/docs/ROBUST_EFFICIENT_OPERATIONS.md`. Treat it as the reliability contract for per-chat isolation, token-efficient routing, queue states, artifact delivery gates, and recovery playbooks.

The optional OverTree personal-WeChat group route remains disabled until a real
test group passes acceptance. It must use the direct monitor's isolated Cloud
branch, a unique private state file, exact group-ID allowlist, exact `self_wxid`,
exact Agent @mention trigger, and exact GUI title guard. Only explicit mentions
and a pre-binding one-time code may enter Cloud; ordinary human group chat must
not trigger the Agent. Never fall through to the local Agent or share the
WeCom/Customer Service credentials, state, or source resolver. Keep Cloud
`/internal/*` routes private over VPN/SSH; restrict media uploads to files
matched by the current message's own media tokens. Preserve retry state for
Cloud requests and group sends, and never forward a one-time binding code into
the Cloud session after an uncertain claim response. Document updates belong
in both `README.md` and `agentic_tools/wechat_gui_agent/docs/ROBUST_EFFICIENT_OPERATIONS.md`.

The explicitly enabled OverTree OCR pilot is a separate visible-GUI ingress:
it uses synthetic rows and must not require native database IDs or receipts.
First-binding discovery may request only exact local group IDs or generated
prefixes in currently visible unread rows; Cloud must validate the registered
UUID and unused live code before admission. Never enumerate dormant groups or
use global search for inbound discovery. Accept binding codes only as standalone
lines. OCR text sends require an exact
title guard and composer-clear verification; uncertain deliveries must stay
persistently held for review, including after restart. Keep other transports'
native receipt checks and per-group state isolation unchanged.

## Commit & Pull Request Guidelines

Use concise imperative commit messages, such as `Add Unity target validation` or `Document BioRender MCP setup`. Pull requests should include a summary, testing performed, linked issues when applicable, and screenshots only for UI-facing changes.

Before finishing any task, classify every dirty path. Intentional source,
design, documentation, manifest, deletion, or move changes must be tested,
committed, and pushed when repository publication is available. Private,
generated, cache, runtime, and byte-preserving local archive paths must receive
the narrowest appropriate `.gitignore` rule. Never finish with an unexplained
dirty path or the generic statement that unrelated changes were left
untouched. Preserve user work while making an explicit commit-or-ignore
decision for it; never commit credentials or private chat/runtime data.

## Security & Configuration Tips

Do not commit `labcanvas.targets.json`, `.aginti/.env`, or generated `output/` files; they may contain local endpoints, tokens, or bulky artifacts. Keep secrets in environment variables such as `BIORENDER_API_KEY`. Treat editor bridges as privileged automation surfaces: review dry-run payloads before enabling live dispatch.

## Agent-Specific Instructions

Before editing, inspect `git status` and preserve unrelated local changes. Prefer repository commands from this file over generic assumptions, and update `README.md` plus this guide whenever CLI behavior or target configuration changes.
