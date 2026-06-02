# EDM — Company Brain, Decision Memory, and SOP Generator

[![Docker Hub](https://img.shields.io/badge/docker-asmittdashh%2Fedm-blue?logo=docker)](https://hub.docker.com/r/asmittdashh/edm)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![status](https://img.shields.io/badge/status-alpha%20v0.0.2-orange)]()
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**EDM** started as Engineering Decision Memory and has grown into a one-stop **Company Brain**: every company has critical know-how scattered across PRs, emails, Slack, support tickets, wikis, and people's heads. EDM pulls that knowledge out, structures it, keeps it current, and exposes two artefacts AI agents and humans both consume — **a queryable causal graph** and **executable skills files**.

It now ships three layers in one self-hosted app:

1. **Engineering Decision Memory (v1)** — the original. Ingests PRs / design docs / diagrams, extracts decisions + assumptions + constraints, surfaces contradictions when new PRs reverse old decisions. Untouched by the v2 expansion.
2. **Company Brain (v2)** — same primitive, every other function. Ingests emails, support tickets, CRM notes, meeting transcripts, wiki pages, and policy docs and turns them into **procedures** (ordered atomic steps + guardrails) tied to a function (sales / support / ops / finance / legal / HR / IT / security / etc.).
3. **SOP Generator (v2)** — form + chatbot intake (~20–30 min) → retrieval over your SOP corpus → synthesis of both a **human-readable SOP** and a paired **executable skills file** (JSON action graph agents can run). Customer-uploaded SOPs rank above seed templates so the generated output looks like *your* documents, not generic boilerplate.

LLM provider is pluggable via the existing abstraction — **Gemini is the default**, **Claude Sonnet** is the swap. OpenAI and OpenRouter still work for v1 paths.

---

## What problem does this solve?

Engineering teams forget *why* things were built the way they are. The pain shows up in three places:

1. **New joinees.** They have to learn the system from people. The actual map of "who decides what, who depends on whom, why is this service the way it is" lives in PRs, Slack threads, and people's heads. Onboarding takes months.
2. **Silent contradictions.** Someone ships a PR that quietly reverses a decision the team made six months ago. Nobody catches it until production breaks or scope balloons.
3. **Stale assumptions.** Decisions get made under constraints (latency budgets, team size, vendor lock-in). The constraints lift, the decisions stay, the codebase calcifies.

Search products (Glean, Notion AI, Slack AI) tell you *what* a doc says. EDM tells you **why** a decision was made, **what** assumptions it depended on, **who** owned it, and whether a new change **contradicts** it. The differentiator is the causal graph + the verifier loop, not the LLM — pluggable providers (OpenAI, Anthropic, Gemini, OpenRouter) make that explicit.

---

## What ships

**Engineering Decision Memory (v1, unchanged):**
- A FastAPI app with a clean Tailwind UI and **role-based session login** (admin / senior / junior)
- A typed Postgres + pgvector schema with full **provenance** (every node and edge points to an `extractions` row → `sources` row)
- LLM provider abstraction supporting **Gemini, Anthropic Claude, OpenAI, and OpenRouter**
- Voyage embeddings for semantic retrieval
- A two-stage **contradiction detector** (embedding retrieve → LLM verify)
- **GitHub OAuth Device Flow** for connecting your org without paste-a-token UX
- Auto **PR backfill** after connect — pulls 50 most-recent PRs and processes them in the background
- A **PR-review bot** webhook that comments on PRs when a contradiction is detected
- An interactive **Cytoscape.js graph**, click-to-detail side panel, edge legend
- Upload pipeline for **Markdown / Mermaid / SVG / PNG / JPG** — images go through a vision LLM
- A **PDF report** with org-member contribution stats, top contributors, decisions, and contradictions

**Company Brain (v2):**
- Procedure extraction across `email_thread`, `support_ticket`, `crm_note`, `meeting_transcript`, `wiki_page`, `policy_doc` source kinds
- 12 built-in **functions** (engineering, product, sales, support, success, marketing, finance, hr, legal, it, operations, security)
- Each procedure stores ordered atomic **steps** (actor + tool + expected outcome) and typed **guardrails** (always / never / condition / escalation / exception / compliance)
- **Procedure contradiction detector** — same two-stage pattern as the engineering layer. When a new policy doc lands that reverses an existing procedure, it shows up at `/procedure-findings` with severity + rationale + provenance.
- **Gmail connector** — read-only OAuth (device flow), pulls threads, runs the brain pipeline on each. UI at `/setup/gmail` (no CLI).
- Brain search UI at `/brain` — semantic retrieval across procedures *and* SOPs

**SOP Generator (v2):**
- Per-session intake combining a **structured form** (org, function, owners, escalation, etc.) and a **20–30 min chatbot** that pulls exception threads (e.g. "uniforms compulsory but Fridays casuals OK, no black")
- Retrieval over a layered corpus: 5 **bundled seed SOPs** + your **uploaded SOPs** + previously **generated SOPs**. Customer uploads outrank seeds.
- Dual-output synthesis: **human-readable SOP markdown** with full SOP sections AND a paired **executable skills file** (JSON action graph + guardrails) at `/api/sops/{id}/skills.json`
- Per-session "consent to train" toggle controls whether the generated SOP gets retained in the corpus for future synthesis

**Tooling:**
- A `Typer` CLI: `edm setup`, `edm db init`, `edm process`, `edm findings`, `edm graph export`, `edm serve`, `edm sop load-seed`, `edm sop upload`, `edm brain ingest-text`

---

## Quickstart — auto-setup (zero terminal commands after launch)

EDM v0.0.2+ ships a fully web-driven setup wizard. You do **not** edit `.env`, **do not** run `edm db init`, **do not** load seed SOPs by hand — the browser does it for you.

```bash
# 1. start Postgres + EDM (one command)
docker compose -f docker-compose.bundle.yml up -d

# 2. open
xdg-open http://127.0.0.1:8088
```

The wizard walks you through, in this exact order:

1. **Database** — paste the connection string. EDM tests it, confirms `pgvector` + `pgcrypto` are present, runs every migration in order, and seeds 5 SOP templates. Idempotent — safe to retry on failure.
2. **Admin account** — username + password. Creates the install-wide root user.
3. **LLM + embeddings** — pick Gemini (default), Claude Sonnet, OpenAI, or OpenRouter. Paste the LLM key. Optionally paste a Voyage key in the same form for production-grade semantic retrieval; leave blank to use the offline stub.
4. **Connectors (optional)** — connect GitHub (engineering source) and/or Gmail (company brain source). Either or both can be skipped and added later from `/setup/connect`.

That's the whole onboarding. No CLI, no env-file editing.

**On Docker Hub the image is `asmittdashh/edm`.** What you'd type if you wanted to run the app container by hand (you usually wouldn't — postgres needs to come too):

```
docker pull asmittdashh/edm:0.0.1
```

---

## Quickstart — build from source

```bash
git clone https://github.com/asmitdash/edm.git
cd edm
cp .env.example .env  # fill in EDM_SESSION_SECRET, EDM_ADMIN_PASSWORD

# Use the same compose file but flip it to build from local Dockerfile:
#   in docker-compose.bundle.yml, comment `image: asmittdashh/edm:0.0.1`
#   and uncomment the `build:` block

docker compose -f docker-compose.bundle.yml up -d --build
```

Or run without Docker:

```bash
# requires Python 3.11+ and Postgres 16 with pgvector
python -m venv .venv && source .venv/bin/activate
pip install -e .
edm db init
edm setup    # interactive: admin + LLM provider + optional GitHub
edm serve    # http://127.0.0.1:8088
```

---

## First-time sign-in walkthrough

The four-step wizard order in v0.0.2+:

1. **Open http://127.0.0.1:8088/.** You'll be redirected to `/setup` — the first-run wizard.
2. **Step 1 — Database.** Paste a Postgres URL (e.g. the one printed by `docker compose`). EDM tests the connection, confirms `pgvector` + `pgcrypto` are present, applies all migrations, and seeds 5 SOP templates. Idempotent — safe to retry.
3. **Step 2 — Admin account.** Pick a username and password. The install-wide root user. Only one admin exists.
4. **Step 3 — LLM + embeddings.** Pick an LLM provider:
   - **Google Gemini** (recommended for the demo): cheap, fast, supports vision. Key at https://aistudio.google.com/apikey.
   - **Anthropic Claude**: best reasoning. Key at https://console.anthropic.com/settings/keys.
   - **OpenAI**: strict JSON-schema output. Key at https://platform.openai.com/api-keys.
   - **OpenRouter**: routes to many models. Key at https://openrouter.ai/keys.

   Optionally paste a Voyage embedding key in the same form for production-grade semantic retrieval (leave blank to use the offline stub). The wizard makes a small validation call before saving. Keys are encrypted at rest with a Fernet key derived from `EDM_SESSION_SECRET`.
5. **Step 4 — Connectors (optional).**
   - **GitHub** (engineering source): OAuth Device Flow. Once connected, EDM auto-ingests the **50 most-recent PRs** in the background.
   - **Gmail** (company-brain source): Google Device Flow, read-only scope. Pulls threads through the brain pipeline. UI at `/setup/gmail`.

   Both are optional and can be added later from `/setup/connect`.
6. **Sign in.** Setup complete → `/login` → use the credentials you set in step 2.

---

## Roles

| Action | admin | senior | junior |
|---|---|---|---|
| View dashboard / decisions / findings / graph / sources / members | ✓ | ✓ | ✓ |
| Run backfill, upload sources, paste decisions, refresh members | ✓ | ✓ | ✗ |
| Connect / disconnect GitHub | ✓ | ✓ | ✗ |
| Invite juniors | ✓ | ✓ | ✗ |
| Invite seniors | ✓ | ✗ | ✗ |
| Change LLM provider / API key | ✓ | ✗ | ✗ |
| Deactivate users | ✓ | ✗ | ✗ |
| View audit log | ✓ | ✗ | ✗ |

To **invite a teammate**: Settings → Team & invites → Generate invite link → copy → share via Slack/email. The link is one-time-use, expires in 48 hours. The invitee sets their own password via the link.

---

## Architecture

```
┌──────────────────┐  ┌────────────────┐  ┌──────────────────┐
│ GitHub PRs       │  │ Slack threads  │  │ Markdown / SVG / │
│ (Device Flow)    │  │ (paste/upload) │  │ PNG / JPG upload │
└────────┬─────────┘  └────────┬───────┘  └─────────┬────────┘
         │                     │                    │
         └─────────────────────┴────────────────────┘
                               │
                               ▼
                       ┌───────────────┐
                       │  sources (PG) │
                       └───────┬───────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │  LLM extraction              │
                │  (OpenAI/Anthropic/Gemini/   │
                │   OpenRouter)                │
                └──────────────┬───────────────┘
                               │
                               ▼
            ┌──────────────────────────────────────┐
            │  decisions / assumptions /           │
            │  constraints / alternatives          │
            │  + edges (typed, confidence,         │
            │  full provenance to extractions)     │
            └─────────────────┬────────────────────┘
                              │
                              ▼
                ┌──────────────────────────┐
                │  Voyage embeddings       │
                │  (pgvector index)        │
                └─────────────┬────────────┘
                              │
                              ▼
        ┌─────────────────────────────────────────┐
        │  Contradiction detector                 │
        │  (cosine retrieval → LLM verifier loop) │
        └────────────────────┬────────────────────┘
                             │
                             ▼
                ┌────────────────────────┐
                │  conflict_findings     │
                └─┬───────────────────┬──┘
                  │                   │
                  ▼                   ▼
    ┌─────────────────────┐ ┌────────────────────────┐
    │ PR-review bot       │ │ Web UI: dashboard,     │
    │ (inline GitHub      │ │ graph, findings, PDF   │
    │ comment)            │ │ report                 │
    └─────────────────────┘ └────────────────────────┘
```

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| App | FastAPI + Starlette `SessionMiddleware` + Jinja2 + Tailwind CDN | Fast, no frontend build for a single-tenant app |
| Auth | `argon2-cffi` password hashing, signed-cookie sessions, Fernet at-rest encryption for stored API keys | Standard cryptographic hygiene |
| Store | Postgres 16 + pgvector | One DB for graph + embeddings; recursive CTEs cover traversal |
| LLM | OpenAI / Anthropic / Gemini / OpenRouter — provider switch lives in the DB | Schema-conformant structured output via tool-use / `response_schema` / OpenAI JSON-schema mode |
| Embeddings | Voyage `voyage-3` (1024-d) | Strong retrieval on technical text |
| CLI | Typer + Rich | Ergonomic local ops |
| Graph viz | Cytoscape.js + `cose-bilkent` (physics) | Click, drag, filter — no frontend build |
| Tests | pytest with stub providers + a real Postgres test DB | Network-free pipeline verification |

---

## Configuration reference (.env)

| Var | Required | Default | Purpose |
|---|---|---|---|
| `EDM_SESSION_SECRET` | **yes** | dev-only placeholder | Signs session cookies, derives the Fernet key for stored API keys. **Rotate before production.** |
| `EDM_ADMIN_USER` / `EDM_ADMIN_PASSWORD` | only used by env-fallback path | `admin` / `admin` | The setup wizard creates the real admin via DB; these env vars are only used when the env-driven legacy path is active |
| `DATABASE_URL` | inside Docker, set automatically | `postgresql+psycopg://edm:edm@postgres:5432/edm` | Postgres connection |
| `EDM_LLM_PROVIDER` | optional | `gemini` | Default for the wizard; users can pick any provider in the UI |
| `EDM_EMBEDDING_PROVIDER` | optional | `voyage` | Currently `voyage` or `stub` (offline tests) |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` | env-fallback only | — | Used only if you bypass the wizard |
| `VOYAGE_API_KEY` | yes for live embeddings | — | Required to extract — embeddings drive retrieval |
| `EDM_HOST` / `EDM_PORT` | optional | `0.0.0.0:8088` | Bind address inside the container |

---

## Operating the stack

```bash
# tail logs
docker compose -f docker-compose.bundle.yml logs -f app

# stop (data persists)
docker compose -f docker-compose.bundle.yml down

# stop + wipe data (fresh setup wizard next time)
docker compose -f docker-compose.bundle.yml down -v

# rebuild from source after a code change
docker compose -f docker-compose.bundle.yml up -d --build --force-recreate
```

Convenience launchers in the repo:

- `scripts/edm-deploy.sh up | down | logs | rebuild` (Linux/macOS/WSL)
- `edm-deploy.cmd up | down | logs | rebuild` (Windows)

---

## Security posture (alpha)

This is a v0.0.1 self-hosted demo. Read this before you deploy to a non-toy environment:

- The default admin credentials in `.env.example` are placeholders. **Always set your own `EDM_SESSION_SECRET` and admin password.**
- Sessions are signed cookies (`SameSite=Lax`). Flip `https_only=True` in `edm/api/server.py` once you put TLS in front.
- Stored API keys and OAuth tokens are encrypted at rest with Fernet, keyed by `EDM_SESSION_SECRET`. Rotating that secret invalidates all stored keys — re-run the setup wizard to re-enter them.
- The PR-review bot verifies the GitHub webhook signature (`X-Hub-Signature-256`) when `GITHUB_WEBHOOK_SECRET` is set. Set it.
- Junior accounts get **read access to everything in the EDM graph**. Provenance links out to GitHub respect GitHub's own ACLs, but EDM does not currently enforce per-team data segmentation. v1 adds team boundaries.

---

## Tests

```bash
EDM_LLM_PROVIDER=stub EDM_EMBEDDING_PROVIDER=stub \
DATABASE_URL=postgresql+psycopg://edm:edm@127.0.0.1:5432/edm_test \
.venv/bin/pytest -v
```

15 tests covering: schema correctness, extraction → graph write, contradiction detection, provenance chain, stub provider behaviour. The stub providers let the suite run network-free.

---

## Company Brain + SOP routes

The auto-setup wizard runs migrations 001 → 005. The new surfaces are:

| Route | Purpose |
|---|---|
| `/procedures` | Browse extracted procedures across all functions |
| `/procedures/{id}` | Drill into one procedure: steps + guardrails |
| `/procedure-findings` | Cross-functional contradictions surfaced by the Brain |
| `/sops` | SOP library: seed + uploaded + generated, filterable |
| `/sops/{id}` | View a single SOP with its paired skills file |
| `/sops/upload` | Upload your existing SOPs (markdown) — seniors / admin only |
| `/sop-gen` | Start a new SOP generation session |
| `/sop-gen/{id}` | The session: form + chat + generate button |
| `/brain` | Semantic search across procedures and SOPs |
| `/setup/database` | Auto-migrate + auto-seed (first run, runnable any time) |
| `/setup/gmail` | Connect Gmail (device flow) and pull threads |
| `/api/sops/{id}/skills.json` | Download the executable skills file |

Bootstrap is automatic — the seed library is loaded the first time you complete `/setup/database`. The CLI commands below remain for power users:

```bash
edm sop load-seed                                                # idempotent re-seed
edm brain ingest-text path/to/policy.md --kind policy_doc        # CLI ingest
```

To pull email threads, connect Gmail at `/setup/gmail` then either click "Pull threads" or POST to `/ingest/gmail`. No CLI required.

## Switching between Gemini and Claude Sonnet

The default is Gemini. Two ways to switch:

- **UI:** `/settings/llm` → pick `Anthropic Claude` → enter the key. Re-validates and persists.
- **CLI / env:** set `EDM_LLM_PROVIDER=anthropic` and provide `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL=claude-sonnet-4-6`.

Same abstraction drives every LLM call — engineering extraction, procedure extraction, chatbot exception extraction, "ask or finish" decision, SOP synthesis, skills-file synthesis. No code change needed to swap.

## What's NOT in v0.0.x

Per the explicit scope decisions during design:

- Slack ingestion (paste-text works, but no OAuth)
- Multi-tenant / cross-company superadmin (single-tenant only)
- Linear / Jira ingestion
- Hosted SaaS — this is self-host only
- Per-team data segmentation (everyone in the install sees everything)
- Live execution of skills files (this build emits them; the agent runtime that consumes them is separate)

---

## License

MIT. See `LICENSE`.

## Contributing

Issues and PRs welcome. Especially:
- Additional ingestion sources
- Better extraction prompts
- Vision-LLM accuracy on architecture diagrams
- Per-team / per-repo permission boundaries

---

*Built by [@asmitdash](https://github.com/asmitdash) — AI Systems Architect intern at Deloitte. Not an official Deloitte product.*
