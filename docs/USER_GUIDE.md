# Relentless User & Operations Guide

Everything you need to customize your job targets globally, run your daily search from Telegram or the Web Command Center, review tailored resumes, inspect live logs, and use the CLI / MCP tools.

> **Return to:** [Main README](../README.md) • [Setup & Deployment Guide](SETUP_GUIDE.md)

---

## First-Time Onboarding & Regional Customization

### Setting Up Your Candidate Master Profile
Relentless **never** uses hardcoded fake/personal resume data in production (`config/master_profile.json` is `.gitignore`d and never shipped in Git). You can configure your profile in either of two synced ways:

1. **Zero-CLI Web UI (Recommended for Cloud):**
   - Sign in -> open **`06 // CONFIG & VAULT`** -> **👤 Candidate Profile**.
   - Fill in your Name, Email, Location, LinkedIn/GitHub, Core Skills, Executive Summary, Experience roles, Education, and Application Defaults — or click **`📋 LOAD STARTER TEMPLATE`** to load `config/master_profile.example.json` into the editor, replace every field with your real history, and click **`💾 SAVE CANDIDATE PROFILE`**.
2. **Local JSON File + Sync CLI:**
   - Copy `config/master_profile.example.json` to `config/master_profile.json`, edit with your details, and run `python manage.py sync`.

*(For test suites or demo previews only, setting `ALLOW_EXAMPLE_PROFILE=1` allows fallback to `config/master_profile.example.json`.)*

### Customizing Models, Locations & Job Boards Globally
Relentless v4 gives you visual control over **AI models**, **composable location rules**, and **all ingestion sources** in **`06 // CONFIG & VAULT`** -> **`⚙️ ENGINE & DISCOVERY CONFIG`** (and in `config/config.yaml`):

1. **AI Models & Embeddings Picker (OpenAI / OpenRouter):**
   - Defaults: `tailor` & `browser_agent` = `openai / gpt-4o`, `embeddings` = `openai / text-embedding-3-small`, `triage` = `openrouter / typesafe/jev-1.14`.
   - Click **`↻ DISCOVER LIVE MODELS`** to list available models for your configured `OPENAI_API_KEY` and `OPENROUTER_API_KEY`, switch providers/models per role (or enter any custom model ID), and save immediately.
2. **Composable Location Policy (`OR` / `AND` / `XOR` / `NOT`) + Live Dry-Tester:**
   - Default policy is strict **Remote-only (Anywhere / Worldwide / Global / India-eligible)**.
   - Combine rules with `OR`, `AND`, or `XOR` (e.g., *Remote globally* `OR` *Hybrid/On-site in Hyderabad/Bengaluru*), set `reject_countries` (`us`, `uk`, `eu`, `canada`, `emea`), and toggle JD geo/work-auth & 5-day-onsite blocker detection.
   - Use the built-in **Live Location Policy Dry-Tester** (`POST /api/location-policy/test`) right inside the form to test any title, location string (e.g., `Indianapolis, IN`, `Remote (US Only)`, `Remote (Worldwide)`), and JD snippet before saving!
3. **Ingestion Sources & Intervals:**
   - Independently toggle **Jobboards (JobSpy)**, **Gmail Alerts (Label-Only)**, **Direct ATS Boards** (Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee with `ETag`/304 & 3x-404 auto-pruning), **Public Remote Feeds** (Remotive/Arbeitnow), and **Bounded Workday CXS** (opt-in, default off).
   - Set `country_indeed` (`india`, `usa`, `uk`, `canada`, `germany`, `australia`, etc.), `max_experience_years`, and per-source poll intervals.
4. Click **`💾 SAVE ENGINE CONFIG`** (validated via Pydantic `AppConfig` and synced to Neon PostgreSQL; timestamp-aware config loading respects newer local `config/config.yaml` edits as well).

---

## Web Command Center Modules (`01`–`08`)

Every module is accessible from the left navigation rail or via direct deep links (`/?view=studio&job=123`, `/?view=okf`). Hold `Ctrl+Click` (Windows/Linux) or `Cmd+Click` (macOS) on any job card to inspect multiple jobs across separate browser tabs.

| Module | What You Do Here |
| --- | --- |
| **`01 // MISSION CONTROL`** | Funnel KPIs, Priority Unreviewed Targets, First-Run Onboarding Banner, and Live Worker Telemetry (`TASK_REGISTRY`). |
| **`02 // JOBS PIPELINE`** | Switch between Kanban board and dense Table view; filter by stage (`UNREVIEWED`, `IN_PROGRESS`, `READY_FOR_REVIEW`, `APPLIED`, `CALLBACK`, `SKIPPED`), Fit Tier, or ATS source. |
| **`03 // APPLICATION STUDIO`** | Side-by-side workspace: Before-vs-After **ATS Scorecard & Diff**, full Job Description, Apply/Outcome Log, **Surgical Resume Revision Prompt**, rendered **Typst PDF preview/download**, live **Playwright form screenshots**, and **Resume JSON**. |
| **`04 // OKF BRAIN`** | Interactive Obsidian-style Concept Graph (`human-reviewed` lime nodes, `machine-confirmed` purple nodes, `unverified` amber nodes), full-text Concept Explorer & Markdown Editor, and Distilled Market Signals. |
| **`05 // ML CALIBRATION`** | Ground-truth `Precision@K` vs. lexical cosine baseline, 95% Wilson Confidence Intervals, Brier Skill Score (BSS), and per-ATS channel approval yield. |
| **`06 // CONFIG & VAULT`** | Visual editors for Candidate Master Profile, Engine & Discovery Config, Automation Pause Controller (1h–7d), Danger Zone DB Reset (`CONFIRM RESET` safeguard), and Raw JSON view. |
| **`07 // LOGS & DIAG`** | Live System Readiness & Env Checklist + streaming secret-redacted server logs with level filters (`ALL`, `ERROR`, `WARN`, `INFO`), keyword search, Pause toggle, Clear, and `.txt` export. |
| **`08 // SYSTEM GUIDE`** | Built-in quick reference, productivity shortcuts, and security architecture overview. |

---

## Telegram Pocket Agent Reference

Relentless turns Telegram into an interactive remote control synced 1:1 with the Web Command Center:

### Interactive Job Workflow
1. **Receive Match Card:** Shows company, title, location, ATS source, calibrated Fit %, Scam Risk %, persona route, and direct link.
   - Buttons: **`🎯 Interested (Tailor & Check Site)`** • **`👎 Skip: Bad Fit`** • **`📝 Skip w/ Reason`** • **`🚫 Skip: Scam`** • **`⭐ Callback!`**
2. **Structured Skip Reasons & Intelligent Learning Gate (Telegram & Web):**
   - Tapping **`📝 Skip w/ Reason`** (or clicking Skip in the Web UI) lets you pick **Experience / Seniority Mismatch**, **Wrong Stack**, **Wrong Location**, **Expired / Closed**, **Scam / Avoid Co**, or write a **Custom Reason** (just like resume edits).
   - Ephemeral skips (like **Expired / Closed** or `"busy today"`) are automatically marked `learn_from_action = FALSE` and excluded from OKF Brain distillation and PAVA calibration so they never degrade future recommendations!
3. **Tailored Package & Site Check:** Clicking **Interested** runs **`TailorGraph`** (full untruncated JD, multi-page depth-preserving Typst PDF, 3-tier skill provenance, fail-loud Jev audit, honest un-clamped ATS scorecard) + **`ApplyAgentGraph`** (Playwright inspection) and updates the card with:
   - Live form preview screenshot + downloadable `.pdf` + direct URL.
   - **If direct form is ready:** **`🚀 Auto-Submit Now`** (requires `CONFIRM_SUBMIT` mode and creates a single-use SHA-256 job-bound token).
   - **If blocked (Aggregator Shell / CAPTCHA / login wall / complex form):** clearly triggers the **Interactive Human Gate**, explains the exact blocker, and gives you the PDF + direct URL to complete cleanly in seconds.
4. **Surgical Resume Tweaks in Seconds:** Tap **`✏️ Request Resume Changes`** (or reply directly to a job card, or send `#<id> <instruction>`, e.g., `#42 emphasize PyTorch latency reduction and add Redis`). Your instruction is obeyed immediately, and OKF Brain extracts the generalizable tailoring principle (**the WHY**) for future roles.

### Telegram Slash Commands

| Command | Action |
| --- | --- |
| `/start` or `/help` | List all available Telegram control commands |
| `/digest` | Push top unreviewed high-fit matches immediately |
| `/status` | Database counts, triaged jobs, human labels (`n`), and active background workers |
| `/logs [level] [limit] [query]` | Tail recent secret-redacted logs & warnings (e.g., `/logs error`, `/logs warning 40`, `/logs workday`) |
| `/brain` | Summary of OKF Market Intelligence (hot skills, hiring velocity, geo trends, edit habits, scam count) |
| `/eval` | Run and display live Precision@K, 95% Wilson CI, and Brier calibration report |
| `/stop_apply` | Pause autonomous Playwright submissions for 24 hours |
| `/apply_status` | Show the 10 most recent Playwright apply/prepare events |
| `/reset_labels` | Soft Reset (clears button clicks, keeps Jev scores — requires inline confirm) |
| `/reset_triage` | Medium Reset (clears scores & labels — requires inline confirm) |
| `/wipe_all` | Hard Reset (deletes all jobs & resets IDs — requires inline confirm) |

---

## Automation Guardrails & Presets

In `config/config.yaml` (and **`06 // CONFIG & VAULT`** with 1-click preset buttons), automation starts in strict **`PREPARE_ONLY`** mode — **submissions are hard-blocked at the runner and LangGraph preflight guard until you explicitly switch to `CONFIRM_SUBMIT`**:

| Preset | `apply_mode` | `daily_cap` | Cooldown | `min_fit_prob` | `max_scam_prob` | Allowed Tiers | Max Required Essays |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **`safe`** *(default)* | `PREPARE_ONLY` | `5` / day | `72h` | `0.70` | `0.30` | `strong_fit`, `dream_role` | `1` |
| **`balanced`** | `CONFIRM_SUBMIT` | `5` / day | `48h` | `0.62` | `0.30` | `strong_fit`, `dream_role`, `moderate_fit` | `2` |
| **`fast`** | `CONFIRM_SUBMIT` | `10` / day | `24h` | `0.55` | `0.35` | `strong_fit`, `dream_role`, `moderate_fit` | `3` |

- **`MANUAL_ONLY`:** Tailors PDFs and surfaces direct links only; zero browser automation runs.
- **`PREPARE_ONLY`** *(alias: `DRY_RUN`)*: Pre-fills forms and captures screenshots only; `submit_application()` returns `DRY_RUN_BLOCKED`.
- **`CONFIRM_SUBMIT`** *(alias: `AUTO_CONFIRM`)*: Submits **only** after explicit human button confirmation generating a job-bound SHA-256 token locked to `pdf_sha256` and `apply_url`, and still respects daily caps, company cooldowns, domain allowlists, and Human Gates on CAPTCHAs/logins.

---

## Observability: Live Logs (`/logs` & Web UI)

Relentless captures structured Python logs + console warnings/errors into a bounded, **automatically secret-redacted** ring buffer (`src/logging_config.py`) alongside live startup readiness diagnostics (`src/readiness.py`):

- **Web UI (`07 // LOGS & DIAG`):**
  - Top panel shows **System Readiness & Env Checklist** (live status for Neon DB, Master Profile, Engine Config, OpenRouter/TypeSafe key, OpenAI key, Telegram, Web Security, and Gmail — with exact fix instructions for any `ERROR` or `WARNING`).
  - Bottom panel streams server logs with level pills (`ALL`, `ERROR`, `WARN`, `INFO`), keyword search, live-stream pause toggle, **Clear Buffer**, and **Export `.txt`**.
- **Telegram `/logs` Command:**
  - `/logs` — last 25 log lines + active setup warnings
  - `/logs error` — filter only `ERROR` entries
  - `/logs warning 40` — last 40 `WARNING` entries
  - `/logs workday` — filter log entries matching `"workday"`

---

## CLI Command Reference (`manage.py`)

| Command | Description |
| --- | --- |
| `python manage.py setup` | Interactive first-time wizard (`.env`, Neon DB, `WEB_API_KEY`, Telegram ID, starter profile) |
| `python manage.py doctor` | Full readiness check (DB connection, pending migrations, API keys, profile status, HTTP retry test) |
| `python manage.py pull` | Git-like pull: download latest `config.yaml` and `master_profile.json` from Neon DB to local files |
| `python manage.py sync [--force]` | Safe push: upload local `master_profile.json` and `config.yaml` to Neon DB (refuses to clobber newer DB edits unless `--force`) |
| `python manage.py run` | Start unified platform: Web Command Center + Telegram Bot + streaming ATS/JobSpy/Gmail/Distill loops |
| `python manage.py run --once` | Execute a single pipeline cycle and exit (fails loudly if required config/profile is missing) |
| `python manage.py run --scrape-only` | Fast cycle polling JobSpy & Gmail only (skips slower direct ATS crawl) |
| `python manage.py run --ats-only` | Scan only configured/discovered direct company ATS endpoints |
| `python manage.py web` | Run only the Web Command Center API & UI server |
| `python manage.py bot` | Run only the Telegram HITL bot listener |
| `python manage.py ingest` | One-off ingestion run (`--ats-only`, `--jobboards-only`, `--gmail-only`, `--sources=...`) |
| `python manage.py distill` | Run OKF Knowledge Brain distillation into Markdown/DB |
| `python manage.py eval` | Print live Jev Precision@K, 95% Wilson CIs, and Brier calibration table (excluding expired skips) |
| `python manage.py calibrate` | Fit & persist LOOCV-validated PAVA calibration model (`n>=30`, `>=10` per class) |
| `python manage.py experiment [start\|complete\|list]` | Start, complete, or inspect tracked before/after A/B experiments (`experiments` table) |
| `python manage.py reset [labels\|triage\|all]` | Reset human labels, triage scores, or wipe all jobs (with confirmation safeguards) |
| `python manage.py health` | Quick DB health check (`SELECT 1;`) |
| `python manage.py knowledge export [dir]` | Export OKF knowledge bundle safely |

---

## Optional MCP Server (`src/mcp_server.py`)

Relentless also exposes a `FastMCP` server (`Relentless-Career-Agent`) over `stdio` or authenticated `http` so AI coding assistants (like Claude Desktop, Cursor, or Antigravity) can inspect top matches (`get_top_job_matches`), trigger resume tailoring (`tailor_resume_pdf`), prepare Playwright forms (`prepare_application`), or pull calibration reports (`get_ml_evaluation_report`).

---

## Safety & Privacy Guarantees

- **Strict Fail-Loud Policy:** Missing required credentials or unconfigured profiles raise explicit errors and log actionable fix steps instead of falling back to fake data.
- **No Unapproved Submissions:** Default apply mode (`PREPARE_ONLY`) fills forms and captures screenshots only and hard-blocks submission; `CONFIRM_SUBMIT` requires explicit button confirmation per application and enforces daily caps, domain allowlists, per-company cooldowns, and Human Gates on CAPTCHAs/logins.
- **Secret Redaction:** API keys (`sk-...`, `sk-or-v1-...`), `Bearer` tokens, Telegram bot tokens, and PostgreSQL passwords in connection URIs are redacted before entering log buffers or Telegram `/logs`.
- **Keep Personal Data Private:** `.gitignore` excludes `.env`, `config/master_profile.json`, `*.pdf`, `/output/`, `/knowledge/`, and `project_env_info.md`. Always verify `git status` before pushing to a public repository.

---

## Running Tests & Linting

```bash
ruff check .
pytest tests/ -q
```
