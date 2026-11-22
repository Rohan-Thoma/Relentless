# Relentless Setup & Deployment Guide

This guide covers every API key, database setup, and deployment option (Railway Direct, Private Netlify + Railway Edge Proxy, and Local Self-Hosting on Linux, macOS, or Windows).

> **Return to:** [Main README](../README.md) • [User & Operations Guide](USER_GUIDE.md)

---

## Prerequisites & Accounts Checklist

Before deploying or running locally, gather the keys below. **Every secret stays in `.env` or your host environment variables — never commit keys or `config/master_profile.json` to Git.**

| Variable | Required? | Purpose | Where to Get It |
| --- | --- | --- | --- |
| `DATABASE_URL` | **Required** | Primary PostgreSQL storage (jobs, settings, OKF knowledge, audit logs) | [Neon](https://neon.tech) |
| `OPENROUTER_API_KEY` | **Required** *(unless `TYPESAFE_API_KEY` is set)* | TypeSafe Jev AI Job Filtering & Hallucination Audit | [OpenRouter Keys](https://openrouter.ai/keys) |
| `WEB_API_KEY` *(or `WEB_ADMIN_PASSWORD`)* | **Required for Cloud/Remote** | Full-screen Web Command Center login password & API key | Generate locally (command below) |
| `OPENAI_API_KEY` | Recommended | Default Resume Tailor (`gpt-4o`), Vision Browser Agent, & `text-embedding-3-small` `pgvector` embeddings *(switchable to OpenRouter in Web UI)* | [OpenAI API Keys](https://platform.openai.com/api-keys) |
| `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` | Recommended | Instant Telegram digests, inline approvals, structured skip reasons, alerts & `/logs` | `@BotFather` & `@userinfobot` |
| `GMAIL_ADDRESS` & `GMAIL_APP_PASSWORD` | Optional | Strict label-only Gmail job alert scanner via IMAP (supports LinkedIn, Naukri, Indeed, Wellfound, Instahyre & direct ATS links) | Google Account App Passwords |
| `TAVILY_API_KEY` | Optional | Optional Stage-3 search fallback for resolving aggregator URLs to direct company ATS career pages | [Tavily](https://tavily.com) |
| `TYPESAFE_API_KEY` | Optional | Direct TypeSafe `/v1/systemone` endpoint instead of OpenRouter | TypeSafe |

---

## Step-by-Step: How to Obtain Every Key

### 1. `DATABASE_URL` (Free Neon PostgreSQL)
1. Create a free account at [neon.tech](https://neon.tech) and click **New Project**.
2. Open your project dashboard -> **Connection Details**.
3. Copy the PostgreSQL connection string (`postgresql://USER:PASSWORD@ep-....aws.neon.tech/neondb?sslmode=require`).
4. Set `DATABASE_URL` to that string. *(Optional: you can also paste the `-pooler` hostname URI into `DATABASE_URL_POOLER` for pooled queries, and Neon keeps `DATABASE_URL` for advisory locks/migrations.)*

### 2. `OPENROUTER_API_KEY` (Low-Cost AI Scoring & Audit Brain)
1. Sign up at [openrouter.ai](https://openrouter.ai).
2. Add a small credit balance under **Credits** (`typesafe/jev-1.14` uses fractions of a cent per job — roughly ~\$0.042 per 1M input tokens and \$0 output tokens).
3. Go to **Keys** -> **Create Key**, copy the `sk-or-v1-...` token, and set `OPENROUTER_API_KEY`.
   - *(Alternative: if you have a direct TypeSafe key, set `TYPESAFE_API_KEY` instead — Relentless routes scoring cleanly to whichever key is configured.)*

### 3. `WEB_API_KEY` (Web UI Security Gate)
Protect your Web Command Center before exposing it on the internet. Generate a strong cryptographic key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

*(Works identically on Linux, macOS, and Windows PowerShell!)*
- Set `WEB_API_KEY` (or `WEB_ADMIN_PASSWORD`) to the output string.
- Whenever you visit your Railway or self-hosted Web URL from a browser, Relentless shows a full-screen **Security Gate**. Paste this `WEB_API_KEY`, click **UNLOCK COMMAND CENTER**, and your browser stores a 7-day `HttpOnly`, `SameSite=Lax` HMAC-SHA256 session cookie (`5` failed login attempts lock out the IP for `15` minutes and send a Telegram alert).
- *(Optional)* Set `WEB_SESSION_SECRET` to a second separate secret if you want cookie signing independent of `WEB_API_KEY`.

### 4. `OPENAI_API_KEY` (Resume Tailoring & PDF Synthesis)
1. Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys).
2. Click **Create new secret key** (`sk-proj-...`) with billing enabled.
3. Set `OPENAI_API_KEY`.
   - Without it, discovery, AI job filtering, Telegram alerts, and the Web UI still run normally — clicking **Tailor Resume** will fail explicitly and log why until you add `OPENAI_API_KEY`.

### 5. `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` (Pocket Agent & `/logs`)
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

### Option A — Railway Direct (Recommended Cloud: 1 Service, Zero Proxy Credit Burn)

> **Why Railway Direct?** Relentless serves its static Web UI, secure login gate, REST API, Playwright automation, background scrapers, and Telegram bot from one container (`python manage.py run`). You don't need Netlify, Vercel, or any extra paid proxy!

1. **Fork** [Rohan-Thoma/Relentless](https://github.com/Rohan-Thoma/Relentless) to your own GitHub account.
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

### Option C — Local Machine / Self-Hosted Server (\$0 Hosting)

Works on **Linux, macOS, and Windows** (strictly OS-independent Python):

```bash
# 1. Clone your fork
git clone https://github.com/Rohan-Thoma/Relentless.git
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

## Next Step

Once your instance is running, continue to the **[User & Operations Guide](USER_GUIDE.md)** to configure your Candidate Master Profile, target locations/ATS boards, Telegram controls, and OKF Knowledge Brain.
