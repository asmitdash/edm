"""FastAPI app: setup wizard + auth + role-gated UI + JSON API + GitHub webhook + PDF report."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Header, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from edm import audit, install_config, setup_flow, users as users_repo
from edm.api import auth
from edm.api.github_bot import handle_pull_request_event
from edm.config import get_settings
from edm.db import session_scope
from edm import jobs
from edm.ingest import github_oauth
from edm.ingest.backfill import spawn_backfill
from edm.ingest.members import member_stats, refresh_members
from edm.ingest.uploads import ingest_upload
from edm.logging import configure_logging, get_logger
from edm.pipeline import process_source
from edm.web.graph_data import graph_payload
from edm.web.report import build_pdf

configure_logging()
log = get_logger(__name__)

settings = get_settings()
app = FastAPI(title="Engineering Decision Memory")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="edm_session",
    max_age=settings.session_max_age_hours * 3600,
    same_site="lax",
    https_only=False,
)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---------- helpers ----------

def _ctx(request: Request, **extra) -> dict:
    role = request.session.get("role")
    user = request.session.get("username")
    base = {"user": user, "role": role}
    base.update(extra)
    return base


def _json_default(o):
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"unserialisable: {type(o).__name__}")


def _json_rows(rows) -> Response:
    return Response(content=json.dumps([dict(r) for r in rows], default=_json_default), media_type="application/json")


def _gate_setup(request: Request) -> RedirectResponse | None:
    """If setup is not complete, redirect to /setup unless we're already there."""
    if setup_flow.is_setup_complete():
        return None
    if request.url.path.startswith(("/setup", "/static", "/api/health")):
        return None
    return RedirectResponse(url="/setup", status_code=303)


# ---------- Setup wizard ----------

@app.get("/setup", response_class=HTMLResponse)
def setup_root(request: Request) -> Response:
    if setup_flow.is_setup_complete():
        return RedirectResponse(url="/login", status_code=303)
    if not users_repo.has_active_admin():
        return RedirectResponse(url="/setup/admin", status_code=303)
    if not install_config.get_llm_config().provider:
        return RedirectResponse(url="/setup/llm", status_code=303)
    return RedirectResponse(url="/setup/github", status_code=303)


@app.get("/setup/admin", response_class=HTMLResponse)
def setup_admin_get(request: Request) -> Response:
    if users_repo.has_any_user():
        return RedirectResponse(url="/setup", status_code=303)
    return templates.TemplateResponse(request, "setup_admin.html", _ctx(request, error=None))


@app.post("/setup/admin")
def setup_admin_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> Response:
    if users_repo.has_any_user():
        raise HTTPException(409, "setup already done")
    if password != password_confirm:
        return templates.TemplateResponse(
            request, "setup_admin.html",
            _ctx(request, error="Passwords don't match"), status_code=400,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request, "setup_admin.html",
            _ctx(request, error="Password must be at least 8 characters"), status_code=400,
        )
    user = setup_flow.create_admin(username, password)
    auth.login_session(request, user)
    return RedirectResponse(url="/setup/llm", status_code=303)


_PROVIDERS_UI = [
    {"value": "gemini",     "label": "Google Gemini",   "default_model": "gemini-2.5-flash",          "hint": "Cheap, fast, strong vision."},
    {"value": "anthropic",  "label": "Anthropic Claude", "default_model": "claude-sonnet-4-5",         "hint": "Best reasoning, tool-use native."},
    {"value": "openai",     "label": "OpenAI",          "default_model": "gpt-4o-mini",               "hint": "Strict JSON-schema output."},
    {"value": "openrouter", "label": "OpenRouter",      "default_model": "openai/gpt-4o-mini",        "hint": "Routes to many models — accepts any provider/model id."},
]


@app.get("/setup/llm", response_class=HTMLResponse)
def setup_llm_get(request: Request) -> Response:
    if not users_repo.has_active_admin():
        return RedirectResponse(url="/setup/admin", status_code=303)
    return templates.TemplateResponse(request, "setup_llm.html", _ctx(request, providers=_PROVIDERS_UI, error=None))


@app.post("/setup/llm")
def setup_llm_post(
    request: Request,
    provider: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(...),
) -> Response:
    user = auth.current_user(request)
    if user is None or user.role != "admin":
        raise HTTPException(403, "admin only")
    result = setup_flow.validate_and_save_llm(
        provider=provider, model=model, api_key=api_key, configured_by=user.id,
    )
    if not result["ok"]:
        return templates.TemplateResponse(
            request, "setup_llm.html",
            _ctx(request, providers=_PROVIDERS_UI, error=f"Key validation failed: {result['error']}"),
            status_code=400,
        )
    return RedirectResponse(url="/setup/github", status_code=303)


@app.get("/setup/github", response_class=HTMLResponse)
def setup_github_get(request: Request) -> Response:
    if not install_config.get_llm_config().provider:
        return RedirectResponse(url="/setup/llm", status_code=303)
    device = request.session.get("setup_device")
    return templates.TemplateResponse(request, "setup_github.html", _ctx(request, device=device, error=None))


@app.post("/setup/github/start")
def setup_github_start(request: Request) -> Response:
    try:
        device = github_oauth.request_device_code()
    except Exception as e:
        return templates.TemplateResponse(
            request, "setup_github.html",
            _ctx(request, device=None, error=f"GitHub error: {e}"), status_code=502,
        )
    request.session["setup_device"] = {
        "device_code": device.device_code,
        "user_code": device.user_code,
        "verification_uri": device.verification_uri,
        "expires_in": device.expires_in,
        "interval": device.interval,
    }
    return RedirectResponse(url="/setup/github", status_code=303)


@app.post("/setup/github/poll")
def setup_github_poll(request: Request, device_code: str = Form(...)) -> Response:
    fake = github_oauth.DeviceCode(
        device_code=device_code, user_code="?", verification_uri="?", expires_in=900, interval=2,
    )
    try:
        token = github_oauth.poll_for_token(fake, max_wait=4)
    except TimeoutError:
        return RedirectResponse(url="/setup/github", status_code=303)
    except Exception as e:
        return templates.TemplateResponse(
            request, "setup_github.html",
            _ctx(request, device=request.session.get("setup_device"), error=f"GitHub error: {e}"),
            status_code=502,
        )
    me = github_oauth.get_authenticated_user(token.access_token)
    request.session["setup_token"] = {"access_token": token.access_token, "scope": token.scope, "github_login": me["login"]}
    request.session.pop("setup_device", None)
    orgs = github_oauth.list_user_orgs(token.access_token)
    return templates.TemplateResponse(
        request, "setup_github_pick.html",
        _ctx(request, github_login=me["login"], orgs=orgs, error=None),
    )


@app.post("/setup/github/finish")
def setup_github_finish(
    request: Request,
    org_login: str = Form(...),
    repo_full: str = Form(""),
) -> Response:
    user = auth.current_user(request)
    tok = request.session.get("setup_token")
    if user is None or tok is None:
        return RedirectResponse(url="/setup", status_code=303)
    install_config.save_github_install(
        github_login=tok["github_login"],
        access_token=tok["access_token"],
        scope=tok["scope"],
        org_login=org_login,
        repo_full=repo_full or None,
        connected_by=user.id,
    )
    audit.log(user_id=user.id, action="github.connect", target=org_login)
    # Auto-kick a backfill: 50 most-recent PRs (or 10/repo across 5 repos for org-wide).
    try:
        job_id = spawn_backfill(
            token=tok["access_token"], org=org_login, repo_full=repo_full or None,
            started_by=user.id, max_prs=50,
        )
        audit.log(user_id=user.id, action="backfill.start", target=str(job_id))
    except Exception as e:
        log.error("setup.backfill_kick_failed", error=str(e))
    request.session.pop("setup_token", None)
    return templates.TemplateResponse(request, "setup_done.html", _ctx(request))


@app.get("/setup/skip")
def setup_skip(request: Request) -> Response:
    user = auth.current_user(request)
    if user:
        audit.log(user_id=user.id, action="setup.skip_github")
    return templates.TemplateResponse(request, "setup_done.html", _ctx(request))


# ---------- Auth ----------

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/", error: str | None = None) -> Response:
    if not setup_flow.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=303)
    if auth.is_authenticated(request):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", _ctx(request, next=next, error=error))


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    user = auth.authenticate_user(username, password)
    if user is None:
        return templates.TemplateResponse(
            request, "login.html",
            _ctx(request, next=next, error="Invalid credentials."), status_code=401,
        )
    auth.login_session(request, user)
    audit.log(user_id=user.id, action="login")
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    user = auth.current_user(request)
    if user:
        audit.log(user_id=user.id, action="logout")
    auth.logout(request)
    return RedirectResponse(url="/login", status_code=303)


# ---------- Setup gate (runs on every request) ----------

@app.middleware("http")
async def setup_gate_middleware(request: Request, call_next):
    redirect = _gate_setup(request)
    if redirect is not None:
        return redirect
    return await call_next(request)


# ---------- Dashboard + lists ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> Response:
    redirect = auth.require_auth(request)
    if redirect:
        return redirect
    with session_scope() as s:
        counts = s.execute(text(
            """
            SELECT
              (SELECT COUNT(*) FROM sources)              AS sources,
              (SELECT COUNT(*) FROM decisions)            AS decisions,
              (SELECT COUNT(*) FROM assumptions)          AS assumptions,
              (SELECT COUNT(*) FROM constraints)          AS constraints,
              (SELECT COUNT(*) FROM conflict_findings WHERE status='open') AS open_findings
            """
        )).mappings().one()
        recent = s.execute(text(
            """
            SELECT cf.severity, cf.rationale, cf.created_at,
                   d.title AS prior_title,
                   sr.url AS triggering_url, sr.title AS triggering_title
            FROM conflict_findings cf
            JOIN decisions d ON d.id = cf.conflicting_decision_id
            JOIN sources sr ON sr.id = cf.triggering_source_id
            WHERE cf.status='open'
            ORDER BY cf.created_at DESC
            LIMIT 5
            """
        )).mappings().all()
    org = install_config.get_github_install().org_login
    stats = member_stats(org) if org else []
    top = sorted(stats, key=lambda m: (-m.n_decisions, -m.n_prs))[:6]
    zeros = [m for m in stats if m.n_prs == 0][:6]
    active = jobs.active_job()
    return templates.TemplateResponse(
        request, "dashboard.html",
        _ctx(request, counts=counts, recent_findings=list(recent),
             top_contributors=top, zero_contributors=zeros, active_job=active,
             github_connected=bool(install_config.get_github_install().org_login)),
    )


@app.get("/decisions", response_class=HTMLResponse)
def decisions_view(request: Request) -> Response:
    if (r := auth.require_auth(request)):
        return r
    with session_scope() as s:
        rows = s.execute(text(
            """
            SELECT d.id, d.title, d.summary, d.status, d.created_at,
                   sr.url AS source_url, sr.title AS source_title
            FROM decisions d
            LEFT JOIN extractions e ON e.id = d.extraction_id
            LEFT JOIN sources sr ON sr.id = e.source_id
            ORDER BY d.created_at DESC
            """
        )).mappings().all()
    return templates.TemplateResponse(request, "decisions.html", _ctx(request, decisions=list(rows)))


@app.get("/findings", response_class=HTMLResponse)
def findings_view(request: Request, status: str = "open") -> Response:
    if (r := auth.require_auth(request)):
        return r
    with session_scope() as s:
        rows = s.execute(text(
            """
            SELECT cf.id, cf.severity, cf.rationale, cf.created_at, cf.posted_pr_comment_url,
                   d.title AS prior_title,
                   sr.url AS triggering_url, sr.title AS triggering_title
            FROM conflict_findings cf
            JOIN decisions d ON d.id = cf.conflicting_decision_id
            JOIN sources sr ON sr.id = cf.triggering_source_id
            WHERE cf.status = :status
            ORDER BY cf.created_at DESC
            """
        ), {"status": status}).mappings().all()
    return templates.TemplateResponse(request, "findings.html", _ctx(request, findings=list(rows), status=status))


@app.get("/sources", response_class=HTMLResponse)
def sources_view(request: Request) -> Response:
    if (r := auth.require_auth(request)):
        return r
    with session_scope() as s:
        rows = s.execute(text(
            """
            SELECT s.id, s.kind, s.external_id, s.title, s.url, s.author, s.occurred_at,
                   COUNT(d.id) AS n_decisions
            FROM sources s
            LEFT JOIN extractions e ON e.source_id = s.id
            LEFT JOIN decisions d ON d.extraction_id = e.id
            GROUP BY s.id
            ORDER BY s.occurred_at DESC
            LIMIT 200
            """
        )).mappings().all()
    return templates.TemplateResponse(request, "sources.html", _ctx(request, sources=list(rows)))


# ---------- Members ----------

@app.get("/members", response_class=HTMLResponse)
def members_view(request: Request) -> Response:
    if (r := auth.require_auth(request)):
        return r
    org = install_config.get_github_install().org_login
    stats = member_stats(org) if org else []
    return templates.TemplateResponse(request, "members.html", _ctx(request, org_login=org, stats=stats))


@app.post("/members/refresh")
def members_refresh(request: Request) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    inst = install_config.get_github_install()
    if not inst.org_login or not inst.access_token:
        raise HTTPException(400, "GitHub not connected")
    n = refresh_members(inst.access_token, inst.org_login)
    audit.log(user_id=auth.current_user(request).id, action="members.refresh", target=inst.org_login, metadata={"count": n})
    return RedirectResponse(url="/members", status_code=303)


@app.post("/ingest/backfill")
def ingest_backfill(request: Request) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    user = auth.current_user(request)
    inst = install_config.get_github_install()
    if not inst.access_token:
        raise HTTPException(400, "GitHub not connected")
    if jobs.active_job():
        return RedirectResponse(url="/sources?error=A+backfill+is+already+running", status_code=303)
    job_id = spawn_backfill(
        token=inst.access_token, org=inst.org_login, repo_full=inst.repo_full,
        started_by=user.id, max_prs=50,
    )
    audit.log(user_id=user.id, action="backfill.start", target=str(job_id))
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_view(request: Request, job_id: UUID) -> Response:
    if (r := auth.require_auth(request)):
        return r
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "job.html", _ctx(request, job=job))


@app.get("/api/jobs/{job_id}")
def api_job(request: Request, job_id: UUID) -> Response:
    if not auth.is_authenticated(request):
        raise HTTPException(401)
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    return JSONResponse(json.loads(json.dumps(job, default=_json_default)))


@app.get("/api/jobs")
def api_jobs(request: Request) -> Response:
    if not auth.is_authenticated(request):
        raise HTTPException(401)
    return JSONResponse(json.loads(json.dumps(jobs.latest_jobs(20), default=_json_default)))


# ---------- Graph ----------

@app.get("/graph", response_class=HTMLResponse)
def graph_view(request: Request) -> Response:
    if (r := auth.require_auth(request)):
        return r
    return templates.TemplateResponse(request, "graph.html", _ctx(request))


@app.get("/api/graph")
def api_graph(request: Request) -> Response:
    if not auth.is_authenticated(request):
        raise HTTPException(401, "Not authenticated")
    return JSONResponse(graph_payload())


# ---------- Uploads ----------

@app.get("/sources/upload", response_class=HTMLResponse)
def upload_get(request: Request, message: str | None = None, error: str | None = None) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    return templates.TemplateResponse(request, "upload.html", _ctx(request, message=message, error=error))


@app.post("/sources/upload")
async def upload_post(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    run_pipeline: str = Form(""),
) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    user = auth.current_user(request)
    raw = await file.read()
    try:
        source_id, kind = ingest_upload(
            filename=file.filename or "upload",
            content_type=file.content_type,
            raw_bytes=raw,
            uploaded_by=user.id,
            title=title or None,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request, "upload.html", _ctx(request, error=f"Upload failed: {e}", message=None), status_code=400,
        )
    audit.log(user_id=user.id, action="upload", target=kind, metadata={"filename": file.filename, "bytes": len(raw)})
    if run_pipeline:
        try:
            process_source(source_id)
        except Exception as e:
            return templates.TemplateResponse(
                request, "upload.html",
                _ctx(request, message=f"Uploaded as {kind}, but pipeline failed: {e}", error=None),
            )
    return templates.TemplateResponse(
        request, "upload.html",
        _ctx(request, message=f"Uploaded ({kind}) and queued for processing.", error=None),
    )


@app.post("/sources/paste")
def paste_post(
    request: Request,
    title: str = Form(...),
    body: str = Form(...),
) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    user = auth.current_user(request)
    raw = body.encode("utf-8")
    source_id, kind = ingest_upload(
        filename=f"{title}.md",
        content_type="text/markdown",
        raw_bytes=raw,
        uploaded_by=user.id,
        title=title,
    )
    audit.log(user_id=user.id, action="paste", target=kind)
    try:
        process_source(source_id)
    except Exception as e:
        return templates.TemplateResponse(
            request, "upload.html",
            _ctx(request, message=f"Saved, but pipeline failed: {e}", error=None),
        )
    return RedirectResponse(url="/decisions", status_code=303)


# ---------- Team / invites ----------

@app.get("/team", response_class=HTMLResponse)
def team_get(request: Request, message: str | None = None) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    return templates.TemplateResponse(
        request, "team.html",
        _ctx(request, invites=users_repo.list_invites(), message=message, base_url=str(request.base_url).rstrip("/")),
    )


@app.post("/team/invite")
def team_invite(request: Request, invite_role: str = Form(...), note: str = Form("")) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    user = auth.current_user(request)
    if invite_role not in ("senior", "junior"):
        raise HTTPException(400, "bad role")
    if invite_role == "senior" and user.role != "admin":
        raise HTTPException(403, "only admin can invite seniors")
    invite = users_repo.create_invite(created_by=user.id, role=invite_role, note=note or None)  # type: ignore[arg-type]
    audit.log(user_id=user.id, action="invite.create", target=invite_role)
    base = str(request.base_url).rstrip("/")
    return RedirectResponse(url=f"/team?message=Invite+ready:+{base}/invite/{invite.token}", status_code=303)


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_get(request: Request, token: str) -> Response:
    invite = users_repo.get_invite_by_token(token)
    if invite is None or invite.redeemed_by or invite.revoked_at:
        raise HTTPException(404, "invite invalid or already used")
    if invite.expires_at < datetime.utcnow().replace(tzinfo=invite.expires_at.tzinfo):
        raise HTTPException(410, "invite expired")
    return templates.TemplateResponse(request, "invite_redeem.html", _ctx(request, invite=invite, error=None))


@app.post("/invite/{token}")
def invite_redeem(request: Request, token: str, username: str = Form(...), password: str = Form(...)) -> Response:
    try:
        user = users_repo.redeem_invite(token, username=username, password=password)
    except ValueError as e:
        invite = users_repo.get_invite_by_token(token)
        return templates.TemplateResponse(
            request, "invite_redeem.html",
            _ctx(request, invite=invite, error=str(e)), status_code=400,
        )
    auth.login_session(request, user)
    audit.log(user_id=user.id, action="invite.redeem", target=user.role)
    return RedirectResponse(url="/", status_code=303)


# ---------- Admin ----------

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, message: str | None = None) -> Response:
    if (r := auth.require_role(request, "admin")):
        return r
    return templates.TemplateResponse(
        request, "admin_users.html",
        _ctx(request, users=users_repo.list_users(), message=message),
    )


@app.post("/admin/users/{user_id}/deactivate")
def admin_deactivate(request: Request, user_id: UUID) -> Response:
    if (r := auth.require_role(request, "admin")):
        return r
    target = users_repo.get_user(user_id)
    if target is None:
        raise HTTPException(404)
    if target.role == "admin":
        raise HTTPException(400, "cannot deactivate the admin")
    users_repo.deactivate_user(user_id)
    audit.log(user_id=auth.current_user(request).id, action="user.deactivate", target=target.username)
    return RedirectResponse(url=f"/admin/users?message=Deactivated+{target.username}", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request) -> Response:
    if (r := auth.require_role(request, "admin")):
        return r
    return templates.TemplateResponse(request, "audit.html", _ctx(request, rows=audit.recent()))


# ---------- Settings ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request) -> Response:
    if (r := auth.require_auth(request)):
        return r
    me = auth.current_user(request)
    is_admin = me and me.role == "admin"
    return templates.TemplateResponse(
        request, "settings.html",
        _ctx(request,
             github=install_config.get_github_install(),
             llm=install_config.get_llm_config(),
             recent_invites=users_repo.list_invites()[:5] if me and me.role in ("senior", "admin") else [],
             users_list=users_repo.list_users() if is_admin else [],
             audit_recent=audit.recent(limit=20) if is_admin else [],
             recent_jobs=jobs.latest_jobs(5)),
    )


@app.get("/settings/github", response_class=HTMLResponse)
def settings_github_get(request: Request) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    return templates.TemplateResponse(request, "setup_github.html", _ctx(request, device=request.session.get("setup_device"), error=None))


@app.post("/settings/github/disconnect")
def settings_github_disconnect(request: Request) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    install_config.disconnect_github()
    audit.log(user_id=auth.current_user(request).id, action="github.disconnect")
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/settings/llm", response_class=HTMLResponse)
def settings_llm_get(request: Request) -> Response:
    if (r := auth.require_role(request, "admin")):
        return r
    return templates.TemplateResponse(request, "setup_llm.html", _ctx(request, providers=_PROVIDERS_UI, error=None))


@app.post("/settings/llm")
def settings_llm_post(request: Request, provider: str = Form(...), model: str = Form(...), api_key: str = Form(...)) -> Response:
    if (r := auth.require_role(request, "admin")):
        return r
    user = auth.current_user(request)
    result = setup_flow.validate_and_save_llm(provider=provider, model=model, api_key=api_key, configured_by=user.id)
    if not result["ok"]:
        return templates.TemplateResponse(
            request, "setup_llm.html",
            _ctx(request, providers=_PROVIDERS_UI, error=f"Validation failed: {result['error']}"),
            status_code=400,
        )
    return RedirectResponse(url="/settings", status_code=303)


# ---------- PDF ----------

@app.get("/report.pdf")
def report_pdf(request: Request) -> Response:
    if (r := auth.require_auth(request)):
        return r
    pdf = build_pdf()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=edm_report.pdf"},
    )


# ---------- API ----------

@app.get("/api/health")
def api_health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/findings")
def api_findings(request: Request, status: str = "open") -> Response:
    if not auth.is_authenticated(request):
        raise HTTPException(401, "Not authenticated")
    with session_scope() as s:
        rows = s.execute(text(
            """
            SELECT cf.id, cf.severity, cf.rationale, cf.created_at, cf.status, cf.posted_pr_comment_url,
                   d.id AS prior_decision_id, d.title AS prior_title,
                   sr.url AS triggering_url, sr.title AS triggering_title, sr.kind AS triggering_kind
            FROM conflict_findings cf
            JOIN decisions d ON d.id = cf.conflicting_decision_id
            JOIN sources sr ON sr.id = cf.triggering_source_id
            WHERE cf.status = :status
            ORDER BY cf.created_at DESC
            """
        ), {"status": status}).mappings().all()
    return _json_rows(rows)


# ---------- Webhook ----------

@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()
    s = get_settings()
    if s.github_webhook_secret:
        if not _verify_signature(body, x_hub_signature_256, s.github_webhook_secret):
            raise HTTPException(401, "Bad signature")
    if x_github_event != "pull_request":
        return JSONResponse({"ignored": x_github_event})
    payload = await request.json()
    if payload.get("action") not in ("opened", "edited", "synchronize", "ready_for_review"):
        return JSONResponse({"ignored_action": payload.get("action")})
    summary = await handle_pull_request_event(payload)
    return JSONResponse(summary)


def _verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
