# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

Avants AI — a Flask-based recruiting platform (Mercor competitor). Employers post jobs and get AI resume scoring + AI voice/chat interviews of candidates before reviewing them; candidates apply, get matched, interview with an AI, and can join a side "Earn" marketplace to complete paid micro-tasks for AI training data.

## Commands

There is no test suite, linter, or build step configured in this repo (no pytest/flake8/ruff/eslint config, no package.json despite `node_modules/` being present — those JS deps are vestigial and unused by the running app).

Local dev:
```bash
pip install -r requirements.txt
python app.py            # runs on $PORT or 8080, debug=True
```

Production (Render, see `render.yaml` / `Procfile`):
```bash
gunicorn app:app
```

Required env vars live in `.env` (loaded via `python-dotenv`): `SECRET_KEY`, `DATABASE_URL`, `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `DAILY_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, `RESET_REDIRECT_URL`. Without `DATABASE_URL` set, the app falls back to a local SQLite file (`database.db`).

## Architecture

**Two entry points into one app.** `app.py` creates the Flask app via `website.create_app()` and adds a handful of top-level, blueprint-free routes for the standalone AI resume tools (resume summarizer, resume/job-match scorer, LinkedIn-to-resume generator, PDF export, Google TTS). Everything else lives under the `website/` package as blueprints, all registered at `url_prefix='/'` in `website/__init__.py` — so each blueprint's own route strings carry their prefix (e.g. `@employer.route('/employer/dashboard')`), not the registration call.

Blueprints (`website/`):
- `auth.py` — email/password + Google (via Supabase) sign-in, email verification (stateless `itsdangerous` tokens), password reset.
- `employer.py` — job CRUD, applicant review/scoring, meeting scheduling, company settings/team invites, billing/subscription, analytics.
- `candidate.py` — job browsing/apply flow, the AI interview chat loop, profile/resume, dashboard, messaging, notifications.
- `earn.py` — the paid micro-task marketplace (separate from hiring) for professionals to rank/rate/fact-check/write AI outputs.
- `admin.py` — internal-only task creation/management and contractor payouts for the Earn marketplace.
- `views.py` — landing page, Stripe webhook, sitemap/robots, video meeting room.

**No migration tool.** There's no Alembic/Flask-Migrate. Schema changes are additive `ALTER TABLE` statements appended to the list in `_run_migrations()` in `website/__init__.py`, each wrapped in try/except so it's a safe no-op once applied — this runs on every app startup. Add new columns there rather than introducing a separate migrations system.

**AI layer is centralized in `ai_services.py`.** All OpenAI calls funnel through it: resume-to-job scoring, resume/LinkedIn text extraction, job-description analysis, job matching/suggestions, the turn-by-turn AI interview (`run_interview_turn`), interview coaching feedback, skills-gap roadmaps, and AI-generated-answer likelihood detection on interview transcripts. Route handlers in `app.py`/`candidate.py`/`employer.py` should call into this module rather than hitting the OpenAI client directly.

**Auth is dual-tracked.** The local `User` table (email/password, Flask-Login) is the primary session mechanism; `supabase_uid` links a user to Supabase for Google OAuth and admin API calls via the service-role client in `website/supabase_client.py`.

**Payments are two separate Stripe flows.** (1) Placement fees: on hire, the candidate confirms, a Stripe **Payment Link** is generated and sent — there's no Checkout Session API call — and `views.py`'s `stripe_webhook` marks `Application.fee_paid` on `checkout.session.completed`. (2) Employer plan billing: `EmployerSubscription` tracks free/starter/pro plans; `PLAN_LIMITS` in `models.py` defines the tier caps but isn't currently enforced by any route — check before assuming it's active.

**Video interviews** go through Daily.co (`daily_utils.py` — thin `httpx` wrapper for room creation + cloud recordings), with `MeetingRoom` records tying a room to an `Application`. `MeetingProposal` lets an employer propose 2–3 time slots for a candidate to accept before a room is created.

**Emails must use `email_utils.external_url()` / `public_base_url()`, not `url_for(_external=True)`.** Emails are opened later, often from a different host than whatever triggered the send, so links are built from `PUBLIC_BASE_URL` config rather than the request's Host header. All transactional email should go through `send_branded_email()` for the shared HTML template.

**Frontend is server-rendered Jinja2**, no build step — Bootstrap 5, Font Awesome, and Google Fonts (Inter) all loaded via CDN in `website/templates/base.html`, which also holds a large inline `<style>` block shared across pages.

**Rate limiting** (`Flask-Limiter`, the `limiter` instance from `website/__init__.py`) is applied per-route on OpenAI-cost-sensitive endpoints — resume matching, resume chat, profile-resume generation, uploads, TTS.

**Local file storage** — `uploads/` and `audio/` are created at startup and gitignored; treat them as ephemeral scratch space, not durable storage.
