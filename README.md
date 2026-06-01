# EDM — your engineering decision Brain

[![Docker Hub](https://img.shields.io/badge/docker-asmittdashh%2Fedm-blue?logo=docker)](https://hub.docker.com/r/asmittdashh/edm)
![status](https://img.shields.io/badge/status-alpha-orange)

**EDM (Engineering Decision Memory)** is a self-hosted "engineering Brain" for your team. It ingests your GitHub PRs, design docs, Mermaid diagrams, and architecture screenshots, uses an LLM to extract every **decision** plus the **assumptions and constraints behind it**, and stores them as a causal graph. When a new PR lands, EDM semantically retrieves prior decisions and asks the LLM whether any are contradicted — surfacing **"this PR conflicts with a decision made 4 months ago"** with click-through provenance to the source artifact.

---

## What problem does this solve?

Engineering teams forget *why* things were built the way they are. The pain shows up in three places:

1. **New joinees.** They have to learn the system from people. The actual map of "who decides what, who depends on whom, why is this service the way it is" lives in PRs, Slack threads, and people's heads. Onboarding takes months.
2. **Silent contradictions.** Someone ships a PR that quietly reverses a decision the team made six months ago. Nobody catches it until production breaks or scope balloons.
3. **Stale assumptions.** Decisions get made under constraints (latency budgets, team size, vendor lock-in). The constraints lift, the decisions stay, the codebase calcifies.

Search products (Glean, Notion AI, Slack AI) tell you *what* a doc says. EDM tells you **why** a decision was made, **what** assumptions it depended on, **who** owned it, and whether a new change **contradicts** it. The differentiator is the causal graph + the verifier loop, not the LLM — pluggable providers (OpenAI, Anthropic, Gemini, OpenRouter) make that explicit.

---

## What ships

- A FastAPI app with a clean Tailwind UI and **role-based session login** (admin / senior / junior)
- A typed Postgres + pgvector schema with full **provenance** (every node and edge points to an `extractions` row → `sources` row)
- LLM provider abstraction supporting **OpenAI, Anthropic Claude, Google Gemini, and OpenRouter**
- Voyage embeddings for semantic retrieval
- A two-stage **contradiction detector** (embedding retrieve → LLM verify)
- **GitHub OAuth Device Flow** for connecting your org without paste-a-token UX
- Auto **PR backfill** after connect — pulls 50 most-recent PRs and processes them in the background
- A **PR-review bot** webhook that comments on PRs when a contradiction is detected
- An interactive **Cytoscape.js graph** with `cose-bilkent` physics, click-to-detail side panel, edge legend, filter chips
- Upload pipeline for **Markdown / Mermaid / SVG / PNG / JPG** — images go through a vision LLM
- A **PDF report** with org-member contribution stats, top contributors chart, decisions, and contradictions
- A `Typer` CLI: `edm setup`, `edm db init`, `edm process`, `edm findings`, `edm graph export`, `edm serve`

---

## Quickstart — pull from Docker Hub

The fastest path. No source checkout, no build.

```bash
# 1. grab the compose file + an env template
curl -fsSL https://raw.githubusercontent.com/asmitdash/edm/main/docker-compose.bundle.yml -o docker-compose.bundle.yml
curl -fsSL https://raw.githubusercontent.com/asmitdash/edm/main/.env.example -o .env
curl -fsSL https://raw.githubusercontent.com/asmitdash/edm/main/scripts/postgres-init.sh -o postgres-init.sh
mkdir -p scripts && mv postgres-init.sh scripts/postgres-init.sh && chmod +x scripts/postgres-init.sh

# 2. fill in .env with at minimum:
#    EDM_SESSION_SECRET (random 64-char string — used to sign sessions and encrypt stored secrets)
#    EDM_ADMIN_PASSWORD (your own choice; the wizard creates the actual admin)
# (LLM and GitHub keys go in via the setup wizard, not the env file)

# 3. start
docker compose -f docker-compose.bundle.yml up -d

# 4. open
xdg-open http://127.0.0.1:8088   # or just paste in a browser
```

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

1. **Open http://127.0.0.1:8088/.** You'll be redirected to `/setup` (the first-run wizard).
2. **Step 1 — Create the admin account.** Pick a username and password. This is the install-wide root user. Only one admin exists.
3. **Step 2 — Pick an LLM provider.**
   - **Google Gemini** (recommended for the demo): cheap, fast, supports vision. Get a key at https://aistudio.google.com/apikey.
   - **OpenAI**: strict JSON-schema output. Get a key at https://platform.openai.com/api-keys.
   - **Anthropic Claude**: best reasoning. Key at https://console.anthropic.com/settings/keys.
   - **OpenRouter**: routes to many models behind one API. Key at https://openrouter.ai/keys.
   - The wizard makes a small validation call before saving. The key is encrypted at rest with a Fernet key derived from `EDM_SESSION_SECRET`.
4. **Step 3 — (Optional) Connect GitHub.** EDM uses GitHub's OAuth Device Flow. Click "Start GitHub Device Flow", get a one-time code, type it at https://github.com/login/device, pick your org, optionally scope to a single repo.
5. **Auto-backfill kicks off.** Once GitHub is connected, EDM ingests the **50 most-recent PRs** in the background. The dashboard shows a live progress bar. Decisions and contradictions populate as it processes.
6. **Sign in.** Once setup is complete, you land on `/login`. Use the username and password you just created.

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

## What's NOT in v0.0.1

Per the explicit scope decisions during design:

- Slack ingestion (paste-text works, but no OAuth)
- Multi-tenant / cross-company superadmin (single-tenant only)
- Linear / Jira ingestion
- Hosted SaaS — this is self-host only
- Per-team data segmentation (everyone in the install sees everything)

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
