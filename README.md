<p align="center">
  <img src="web/logos/1_Relentless_logo_gradient_lime.png" alt="Relentless Logo" width="210" />
</p>

<h1 align="center">RELENTLESS</h1>

<p align="center">
  <strong>Autonomous Multi-Source Job Discovery, System-1 AI Triage, Zero-Hallucination Resume Tailoring &amp; Human-in-the-Loop Command Center</strong>
</p>

<p align="center">
  <code>Python 3.11+</code> &bull;
  <code>PostgreSQL (Neon)</code> &bull;
  <code>OpenRouter / TypeSafe Jev</code> &bull;
  <code>OpenAI + Typst PDF</code> &bull;
  <code>Playwright</code> &bull;
  <code>Zero-Dependency Web HUD</code> &bull;
  <code>Telegram Bot</code>
</p>

---

## Why Relentless?

Job searching breaks down at three points: discovering fresh postings across dozens of ATS portals before they saturate, separating real engineering roles from ghost jobs and spam, and tailoring an ATS-clean resume without fabricating experience.

**Relentless** automates the grind while keeping **you** strictly in control of every submission:

1. **Multi-Source Discovery & SHA-256 Deduplication** — Polls Greenhouse, Lever, Ashby, Workday CXS, SmartRecruiters, Recruitee, JobSpy boards (LinkedIn / Indeed / Naukri), and optional Gmail job alert labels directly into PostgreSQL.
2. **System-1 AI Triage (`Jev`)** — Evaluates scam/ghost-job risk, fit tier (`dream_role` to `reject`), calibrated fit probability, and target resume persona using your private Candidate Master Profile and learned OKF knowledge rules.
3. **Zero-Hallucination Tailored Resume PDFs (`System-2 + Typst`)** — Generates tailored bullets anchored exclusively in your verified roles, verifies zero fabrication via a second Jev hallucination pass, and renders an ATS-ready PDF named for you.
4. **Human-in-the-Loop Safety Gate** — Prepares Playwright ATS pre-checks with form screenshots and pauses before final submission (`DRY_RUN` or `AUTO_CONFIRM`). Nothing ever submits without your explicit approval via the **Web Command Center** or **Telegram**.
5. **Continuous Learning (`OKF Brain` + `Eval Harness`)** — Distills your approvals, rejections, and callbacks into queryable Markdown knowledge graphs and measures Precision@K, 95% Wilson confidence intervals, and Brier calibration scores.
6. **Built-In Diagnostics & Live Logs** — Inspect system readiness, missing environment variables, and secret-redacted server logs anytime from **`07 // LOGS & DIAG`** in the Web UI or **`/logs`** in Telegram.

---

## Architecture Overview

```text
  ATS Boards (Greenhouse/Lever/Ashby/Workday/SmartRecruiters/Recruitee)
+ JobSpy (LinkedIn / Indeed / Naukri) + Gmail Alert Scanner
                         │
                         ▼ (SHA-256 Deduplicated)
                 PostgreSQL (Neon) ◄──── App Settings & Master Profile (DB-synced)
                         │
                         ▼
         System-1 Jev Triage (OpenRouter / TypeSafe)
         ├── Scam Gate + Cosine Baseline + Calibrated Fit Score
         └── OKF Knowledge Graph Context
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
   Web Command Center              Telegram HITL Bot
   (01–08 HUD + Auth Gate)         (/digest, /status, /logs, Inline Buttons)
          │                             │
          └──────────────┬──────────────┘
                         ▼
      System-2 Tailor (OpenAI + Typst) ──▶ Playwright Pre-Check ──▶ Human Confirm & Apply
```

---

## Prerequisites & Accounts Checklist

Before deploying or running locally, gather the API keys below. **Every secret stays in `.env` or your host environment variables — never commit keys or `config/master_profile.json` to Git.**

| Variable | Required? | Purpose | Where to Get It |
| --- | --- | --- | --- |
| `DATABASE_URL` | **Required** | Primary PostgreSQL storage (jobs, settings, knowledge, audit logs) | [Neon](https://neon.tech) |
| `OPENROUTER_API_KEY` | **Required** *(unless `TYPESAFE_API_KEY` is set)* | System-1 Jev AI Triage & Hallucination Audit | [OpenRouter Keys](https://openrouter.ai/keys) |
| `WEB_API_KEY` | **Required for Cloud/Remote** | Full-screen Web Command Center login password & API key | Generate locally (command below) |
| `OPENAI_API_KEY` | Recommended | Surgical Resume Tailor (`.pdf` generation) & LLM form fallback | [OpenAI API Keys](https://platform.openai.com/api-keys) |
| `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` | Recommended | Instant Telegram digests, inline approvals, alerts & `/logs` | `@BotFather` & `@userinfobot` |
| `GMAIL_ADDRESS` & `GMAIL_APP_PASSWORD` | Optional | Scan Gmail job alert emails via IMAP | Google Account App Passwords |
| `TYPESAFE_API_KEY` | Optional | Direct TypeSafe `/v1/systemone` endpoint instead of OpenRouter | TypeSafe |

---

## Step-by-Step: How to Obtain Every Key

### 1. `DATABASE_URL` (Free Neon PostgreSQL)
1. Create a free account at [neon.tech](https://neon.tech) and click **New Project**.
2. Open your project dashboard -> **Connection Details**.
3. Copy the PostgreSQL connection string (`postgresql://USER:PASSWORD@ep-....aws.neon.tech/neondb?sslmode=require`).
4. Set `DATABASE_URL` to that string. *(Optional: you can also paste the `-pooler` hostname URI into `DATABASE_URL_POOLER` for pooled queries, and Neon keeps `DATABASE_URL` for advisory locks/migrations.)*

### 2. `OPENROUTER_API_KEY` (AI Triage Brain)
1. Sign up at [openrouter.ai](https://openrouter.ai).
2. Add a small credit balance under **Credits** (System-1 Jev Decisions uses fractions of a cent per job).
3. Go to **Keys** -> **Create Key**, copy the `sk-or-v1-...` token, and set `OPENROUTER_API_KEY`.
   - *(Alternative: if you have a direct TypeSafe key, set `TYPESAFE_API_KEY` instead — Relentless routes triage cleanly to whichever key is configured.)*

### 3. `WEB_API_KEY` (Web UI Security Gate)
Protect your Web Command Center before exposing it on the internet. Generate a strong cryptographic key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

*(Works identically on Linux, macOS, and Windows PowerShell!)*
- Set `WEB_API_KEY` to the output string.
- Whenever you visit your Railway or self-hosted Web URL from a browser, Relentless shows a full-screen **Security Gate**. Paste this `WEB_API_KEY`, click **UNLOCK COMMAND CENTER**, and your browser stores an `HttpOnly`, `SameSite=Lax` HMAC-SHA256 session cookie (`5` failed login attempts lock out the IP for `15` minutes).
- *(Optional)* Set `WEB_SESSION_SECRET` to a second separate secret if you want cookie signing independent of `WEB_API_KEY`.

### 4. `OPENAI_API_KEY` (Resume Tailoring & PDF Synthesis)
1. Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys).
2. Click **Create new secret key** (`sk-proj-...`) with billing enabled.
3. Set `OPENAI_API_KEY`.
   - Without it, discovery, triage, Telegram alerts, and the Web UI still run normally — clicking **Tailor Resume** will fail explicitly and log why until you add `OPENAI_API_KEY`.

### 5. `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` (Mobile HITL & `/logs`)
1. Open Telegram and start a chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, choose a display name and username (ending in `bot`), and copy the HTTP API token (`123456789:AA...`) into `TELEGRAM_BOT_TOKEN`.
3. **Important:** Open a chat with your newly created bot and press **Start** (or send `hello`), otherwise Telegram bots cannot message you first.
4. Get your numeric `TELEGRAM_CHAT_ID`:
   - Message **[@userinfobot](https://t.me/userinfobot)** on Telegram and copy your numeric **Id**, OR
   - Leave `TELEGRAM_CHAT_ID` blank during local `python manage.py setup` and Relentless will auto-detect it after you message your bot!

### 6. `GMAIL_ADDRESS` & `GMAIL_APP_PASSWORD` (Optional Job Alert Scanner)
Only needed if you want Relentless to ingest job alert emails:
1. Enable **2-Step Verification** on your Google Account (`myaccount.google.com/security`).
2. Search for **App passwords**, create one named `Relentless`, and copy the 16-character passcode into `GMAIL_APP_PASSWORD`.
3. Set `GMAIL_ADDRESS` to your Gmail address.
4. In Gmail, create a filter that applies your configured label (default: `professional-job_alerts`) to job alert emails, OR change `gmail_scanner.label` in **`06 // CONFIG & VAULT`**.
5. If you don't want Gmail scanning, disable **Enable Gmail Alerts Scanner** in **`06 // CONFIG & VAULT`** -> **Engine & Discovery Config**.

---

## Deployment Options (Pick One)

### Option A — Railway Direct (Recommended: 1 Service, Zero Proxy Credit Burn)

> **Why Railway Direct?** Relentless serves its static Web UI, secure login gate, REST API, Playwright automation, background scrapers, and Telegram bot from one container (`python manage.py run`). You don't need Netlify, Vercel, or any extra paid proxy!

1. **Fork** this repository to your own GitHub account.
2. Create a new project on [Railway](https://railway.app) -> **Deploy from GitHub repo** -> pick your fork.
   - Railway detects `railway.json` and builds using the included `Dockerfile` (`python:3.11-slim` + Playwright Chromium).
3. In Railway -> **Variables**, add:
   - `DATABASE_URL`
   - `OPENROUTER_API_KEY`
   - `WEB_API_KEY`
   - `OPENAI_API_KEY` *(recommended)*
   - `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` *(recommended)*
   - *(Optional)* `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `POLL_INTERVAL_HOURS=1.0`
4. Open Railway **Settings -> Networking -> Generate Domain** (creates `https://<your-app>.up.railway.app`).
5. Visit `https://<your-app>.up.railway.app`:
   - Enter your `WEB_API_KEY` on the full-screen **Security Gate** to sign in.
   - If this is a fresh install, you will see the **First-Run Onboarding Banner** prompting you to complete your **Candidate Master Profile** right inside **`06 // CONFIG & VAULT`** — **no local CLI required!**

### Option B — Private Netlify Frontend + Railway Backend (Split Edge Proxy)

Use this option only if you want your UI gated behind a Netlify Private Site / Password / SSO layer.

> **Netlify Free Credit Warning:** Netlify's free plan deducts build/deploy credits per production deploy (~15 credits/deploy on the usage dashboard). If Netlify auto-builds on every push, frequent commits can burn monthly credits quickly. Prefer **Option A (Railway Direct)** or pause Netlify auto-publishing while iterating!

1. Deploy the backend on **Railway** (same steps 1–4 as Option A).
   - Set `WEB_API_KEY` on Railway.
   - Set `WEB_CORS_ORIGIN` on Railway to your Netlify origin (`https://your-site.netlify.app`), or leave `*`.
2. Create a Netlify site from your fork with these **Build settings** (`netlify.toml` handles edge routing automatically):
   - **Base directory:** `/`
   - **Build command:** *(leave empty)*
   - **Publish directory:** `web`
   - **Functions directory:** *(leave empty / default — do NOT point to `api/api_proxy`; the Edge Function lives in `netlify/edge-functions/api_proxy.ts`)*
3. In Netlify -> **Environment variables**, set **both**:
   - `RELENTLESS_BACKEND_URL` = `https://<your-app>.up.railway.app` *(exact Railway public URL)*
   - `WEB_API_KEY` = *(same `WEB_API_KEY` as Railway — injected server-side into `X-Relentless-Api-Key`)*
4. Deploy Netlify. Note that `api_proxy.ts` **fails loudly** (`HTTP 500 RELENTLESS_BACKEND_URL_MISSING`) if `RELENTLESS_BACKEND_URL` is omitted.

### Option C — Local Machine / Self-Hosted Server

Works on **Linux, macOS, and Windows** (strictly OS-independent Python):

```bash
# 1. Clone your fork
git clone https://github.com/<your-username>/Relentless.git
cd Relentless

# 2. Create and activate a virtual environment
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1

# 3. Install dependencies & Chromium for Playwright
pip install -r requirements.txt
playwright install chromium

# 4. Run the interactive onboarding wizard (creates .env, initializes Neon DB,
#    auto-generates WEB_API_KEY, auto-detects TELEGRAM_CHAT_ID, and personalizes profile)
python manage.py setup

# 5. Verify system readiness & connectivity
python manage.py doctor

# 6. Start the unified platform (Web UI + API + Telegram Bot + Background Pipeline)
python manage.py run
```

Then open **http://127.0.0.1:8000** in your browser! *(Tip: to launch ONLY the Web Command Center without background scrapers/Telegram, run `python manage.py web`.)*

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

### Customizing Locations & Job Boards Globally
Relentless works for **any city or country** (US, UK, EU, Canada, India, APAC, or Remote):
1. Open **`06 // CONFIG & VAULT`** -> **`⚙️ ENGINE & DISCOVERY CONFIG`**.
2. Update:
   - **Candidate Targeting -> Allowed Locations:** e.g., `["remote", "austin", "san francisco", "usa"]` or `["london", "uk", "remote"]`.
   - **Jobboards -> Jobboard Sites:** e.g., `["linkedin", "indeed", "glassdoor"]` *(add/keep `naukri` if targeting India)*.
   - **Jobboards -> Search Terms & Locations:** roles and cities you want JobSpy to query.
   - **Jobboards -> Indeed Country:** set `india`, `usa`, `uk`, `canada`, `germany`, `australia`, etc.
   - **Direct ATS Company Targets:** add Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, and Workday CXS targets.
3. Click **`💾 SAVE ENGINE CONFIG`** (validated via Pydantic `AppConfig` and saved to PostgreSQL immediately).

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
| `python manage.py sync` | Push local gitignored `config/master_profile.json` and `config/config.yaml` to PostgreSQL |
| `python manage.py run` | Start unified platform: Web Command Center + Telegram Bot + streaming ATS/JobSpy/Gmail/Distill loops |
| `python manage.py run --once` | Execute a single pipeline cycle and exit (fails loudly if required config/profile is missing) |
| `python manage.py run --scrape-only` | Fast cycle polling JobSpy & Gmail only (skips slower direct ATS crawl) |
| `python manage.py web` | Run only the Web Command Center API & UI server |
| `python manage.py bot` | Run only the Telegram HITL bot listener |
| `python manage.py ingest` | One-off ingestion run (`--ats-only`, `--jobboards-only`, `--gmail-only`, `--sources=...`) |
| `python manage.py distill` | Run OKF Knowledge Brain distillation into Markdown/DB |
| `python manage.py eval` | Print live Jev Precision@K, 95% Wilson CIs, and Brier calibration table |
| `python manage.py reset [labels\|triage\|all]` | Reset human labels, triage scores, or wipe all jobs (with confirmation safeguards) |
| `python manage.py health` | Quick DB health check (`SELECT 1;`) |

---

## Safety & Privacy Guarantees

- **Strict Fail-Loud Policy:** Missing required credentials or unconfigured profiles raise explicit errors and log actionable fix steps instead of falling back to fake data.
- **No Unapproved Submissions:** Default apply mode (`DRY_RUN`) fills forms and captures screenshots only; `AUTO_CONFIRM` requires explicit button confirmation per application and enforces daily caps, domain allowlists, and per-company cooldowns.
- **Secret Redaction:** API keys (`sk-...`, `sk-or-v1-...`), `Bearer` tokens, Telegram bot tokens, and PostgreSQL passwords in connection URIs are redacted before entering log buffers or Telegram `/logs`.
- **Keep Personal Data Private:** `.gitignore` excludes `.env`, `config/master_profile.json`, `*.pdf`, `/output/`, `/knowledge/`, and `project_env_info.md`. Always verify `git status` before pushing to a public repository.

---

## Running Tests & Linting

```bash
ruff check .
pytest tests/ -q
```
