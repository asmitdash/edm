"""API + UI routes for the SOP Generator and Company Brain.

Mounted on the main FastAPI app via include_router(sop_router).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from edm.api import auth
from edm.brain.retrieval import find_similar_procedures, find_similar_sops
from edm.db import session_scope
from edm.sop import chatbot, corpus, sessions as session_repo, skills as skills_repo, synth
from edm.sop.schemas import SOPFormData

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


sop_router = APIRouter()


def _ctx(request: Request, **extra) -> dict:
    base = {"user": request.session.get("username"), "role": request.session.get("role")}
    base.update(extra)
    return base


def _json_default(o):
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"unserialisable: {type(o).__name__}")


# ============================================================
# Procedures (Company Brain view)
# ============================================================

@sop_router.get("/procedures", response_class=HTMLResponse)
def procedures_view(request: Request, function: str | None = None) -> Response:
    if (r := auth.require_auth(request)):
        return r
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT p.id, p.title, p.summary, p.owner_role, p.status,
                       p.created_at, f.slug AS function_slug, f.label AS function_label,
                       (SELECT COUNT(*) FROM procedure_steps ps WHERE ps.procedure_id = p.id) AS n_steps,
                       (SELECT COUNT(*) FROM procedure_guardrails pg WHERE pg.procedure_id = p.id) AS n_guardrails
                FROM procedures p
                LEFT JOIN functions f ON f.id = p.function_id
                WHERE (:f IS NULL OR f.slug = :f)
                ORDER BY p.updated_at DESC
                LIMIT 200
                """
            ),
            {"f": function},
        ).mappings().all()
        functions_list = s.execute(
            text("SELECT slug, label FROM functions ORDER BY label")
        ).mappings().all()
    return templates.TemplateResponse(
        request, "procedures.html",
        _ctx(request,
             procedures=list(rows),
             functions=list(functions_list),
             selected_function=function),
    )


@sop_router.get("/procedures/{procedure_id}", response_class=HTMLResponse)
def procedure_detail(request: Request, procedure_id: UUID) -> Response:
    if (r := auth.require_auth(request)):
        return r
    with session_scope() as s:
        proc = s.execute(
            text(
                """
                SELECT p.id, p.title, p.summary, p.trigger_description, p.owner_role,
                       p.status, p.created_at, f.slug AS function_slug, f.label AS function_label,
                       p.sop_id
                FROM procedures p
                LEFT JOIN functions f ON f.id = p.function_id
                WHERE p.id = :id
                """
            ),
            {"id": procedure_id},
        ).mappings().first()
        if not proc:
            raise HTTPException(404)
        steps = s.execute(
            text(
                """
                SELECT step_index, instruction, actor_role, tool_or_system, expected_outcome
                FROM procedure_steps WHERE procedure_id = :id
                ORDER BY step_index
                """
            ),
            {"id": procedure_id},
        ).mappings().all()
        guardrails = s.execute(
            text(
                """
                SELECT kind, statement, severity, condition_expr
                FROM procedure_guardrails WHERE procedure_id = :id
                """
            ),
            {"id": procedure_id},
        ).mappings().all()
    return templates.TemplateResponse(
        request, "procedure_detail.html",
        _ctx(request, procedure=proc, steps=list(steps), guardrails=list(guardrails)),
    )


# ============================================================
# SOP corpus (library view)
# ============================================================

@sop_router.get("/sops", response_class=HTMLResponse)
def sops_view(request: Request, source_kind: str | None = None, function: str | None = None) -> Response:
    if (r := auth.require_auth(request)):
        return r
    rows = corpus.list_sops(source_kind=source_kind, function_slug=function)
    return templates.TemplateResponse(
        request, "sops.html",
        _ctx(request, sops=rows, selected_kind=source_kind, selected_function=function),
    )


@sop_router.get("/sops/{sop_id}", response_class=HTMLResponse)
def sop_detail(request: Request, sop_id: UUID) -> Response:
    if (r := auth.require_auth(request)):
        return r
    sop = corpus.get_sop(sop_id)
    if not sop:
        raise HTTPException(404)
    sf = skills_repo.latest_for_sop(sop_id)
    return templates.TemplateResponse(
        request, "sop_detail.html",
        _ctx(request, sop=sop, skills_file=sf),
    )


@sop_router.get("/api/sops/{sop_id}/skills.json")
def sop_skills_json(request: Request, sop_id: UUID) -> Response:
    if not auth.is_authenticated(request):
        raise HTTPException(401)
    sf = skills_repo.latest_for_sop(sop_id)
    if not sf:
        raise HTTPException(404)
    return Response(content=json.dumps(sf.body, default=_json_default, indent=2),
                    media_type="application/json")


@sop_router.get("/sops/upload", response_class=HTMLResponse)
def sop_upload_get(request: Request, error: str | None = None, message: str | None = None) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    with session_scope() as s:
        functions_list = s.execute(text("SELECT slug, label FROM functions ORDER BY label")).mappings().all()
    return templates.TemplateResponse(
        request, "sop_upload.html",
        _ctx(request, functions=list(functions_list), error=error, message=message),
    )


@sop_router.post("/sops/upload")
async def sop_upload_post(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    function_slug: str = Form(...),
    org_label: str = Form(""),
    industry: str = Form(""),
    confidentiality: str = Form("internal"),
) -> Response:
    if (r := auth.require_role(request, "senior", "admin")):
        return r
    user = auth.current_user(request)
    raw = await file.read()
    try:
        body = raw.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(400, f"Could not read file: {e}")
    sop_id = corpus.upload_sop(
        title=title,
        body_markdown=body,
        function_slug=function_slug,
        org_label=org_label or None,
        industry=industry or None,
        confidentiality=confidentiality,
        created_by=user.id if user else None,
    )
    return RedirectResponse(url=f"/sops/{sop_id}", status_code=303)


# ============================================================
# SOP generation sessions
# ============================================================

@sop_router.get("/sop-gen", response_class=HTMLResponse)
def sopgen_index(request: Request) -> Response:
    if (r := auth.require_auth(request)):
        return r
    user = auth.current_user(request)
    sessions = session_repo.list_sessions(initiated_by=user.id if user else None, limit=20)
    with session_scope() as s:
        functions_list = s.execute(text("SELECT slug, label FROM functions ORDER BY label")).mappings().all()
    return templates.TemplateResponse(
        request, "sop_gen_index.html",
        _ctx(request, sessions=sessions, functions=list(functions_list)),
    )


@sop_router.post("/sop-gen/new")
def sopgen_new(
    request: Request,
    function_slug: str = Form(...),
    title: str = Form(""),
    org_label: str = Form(""),
    industry: str = Form(""),
    consent_to_train: str = Form(""),
) -> Response:
    if (r := auth.require_auth(request)):
        return r
    user = auth.current_user(request)
    sess = session_repo.create_session(
        initiated_by=user.id,
        function_slug=function_slug,
        title=title or None,
        org_label=org_label or None,
        industry=industry or None,
        consent_to_train=bool(consent_to_train),
    )
    return RedirectResponse(url=f"/sop-gen/{sess.id}", status_code=303)


@sop_router.get("/sop-gen/{session_id}", response_class=HTMLResponse)
def sopgen_view(request: Request, session_id: UUID) -> Response:
    if (r := auth.require_auth(request)):
        return r
    sess = session_repo.get_session(session_id)
    if not sess:
        raise HTTPException(404)
    msgs = session_repo.get_messages(session_id)
    with session_scope() as s:
        functions_list = s.execute(text("SELECT slug, label FROM functions ORDER BY label")).mappings().all()
    return templates.TemplateResponse(
        request, "sop_gen_session.html",
        _ctx(request, sess=sess, messages=msgs, functions=list(functions_list)),
    )


@sop_router.post("/sop-gen/{session_id}/form")
def sopgen_form(
    request: Request,
    session_id: UUID,
    org_name: str = Form(""),
    industry: str = Form(""),
    function_slug: str = Form(""),
    title: str = Form(""),
    purpose: str = Form(""),
    scope: str = Form(""),
    in_charge_role: str = Form(""),
    escalation_contact: str = Form(""),
    expected_frequency: str = Form(""),
    primary_systems: str = Form(""),
    compliance_tags: str = Form(""),
) -> Response:
    if (r := auth.require_auth(request)):
        return r
    form = SOPFormData(
        org_name=org_name or None,
        industry=industry or None,
        function_slug=function_slug or None,
        title=title or None,
        purpose=purpose or None,
        scope=scope or None,
        in_charge_role=in_charge_role or None,
        escalation_contact=escalation_contact or None,
        expected_frequency=expected_frequency or None,
        primary_systems=[x.strip() for x in primary_systems.split(",") if x.strip()],
        compliance_tags=[x.strip() for x in compliance_tags.split(",") if x.strip()],
    )
    session_repo.attach_form(session_id, form)
    # kickoff first chatbot question
    try:
        chatbot.kickoff_message(session_id)
    except Exception:
        pass
    return RedirectResponse(url=f"/sop-gen/{session_id}", status_code=303)


@sop_router.post("/sop-gen/{session_id}/chat")
def sopgen_chat(request: Request, session_id: UUID, message: str = Form(...)) -> Response:
    if (r := auth.require_auth(request)):
        return r
    if not message.strip():
        return RedirectResponse(url=f"/sop-gen/{session_id}", status_code=303)
    chatbot.take_user_turn(session_id, message)
    return RedirectResponse(url=f"/sop-gen/{session_id}", status_code=303)


@sop_router.post("/sop-gen/{session_id}/generate")
def sopgen_generate(request: Request, session_id: UUID) -> Response:
    if (r := auth.require_auth(request)):
        return r
    summary = synth.generate_sop_for_session(session_id)
    return RedirectResponse(url=f"/sops/{summary['sop_id']}", status_code=303)


@sop_router.get("/api/sop-gen/{session_id}")
def sopgen_api(request: Request, session_id: UUID) -> Response:
    if not auth.is_authenticated(request):
        raise HTTPException(401)
    sess = session_repo.get_session(session_id)
    if not sess:
        raise HTTPException(404)
    payload: dict[str, Any] = {
        "id": str(sess.id),
        "status": sess.status,
        "title": sess.title,
        "org_label": sess.org_label,
        "industry": sess.industry,
        "function_slug": sess.function_slug,
        "form_data": sess.form_data,
        "derived_exceptions": sess.derived_exceptions,
        "n_messages": len(session_repo.get_messages(session_id)),
        "generated_sop_id": str(sess.generated_sop_id) if sess.generated_sop_id else None,
        "generated_skills_file_id": str(sess.generated_skills_file_id) if sess.generated_skills_file_id else None,
    }
    return JSONResponse(payload)


# ============================================================
# Brain search (cross-functional retrieval)
# ============================================================

@sop_router.get("/procedure-findings", response_class=HTMLResponse)
def procedure_findings_view(request: Request, status: str = "open") -> Response:
    if (r := auth.require_auth(request)):
        return r
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT pcf.id, pcf.severity, pcf.rationale, pcf.created_at, pcf.status,
                       p_prior.id   AS prior_id,   p_prior.title AS prior_title,
                       p_new.id     AS new_id,     p_new.title   AS new_title,
                       sr.url AS triggering_url,   sr.title AS triggering_title,
                       sr.kind AS triggering_kind
                FROM procedure_conflict_findings pcf
                JOIN procedures p_prior ON p_prior.id = pcf.conflicting_procedure_id
                LEFT JOIN procedures p_new ON p_new.id = pcf.new_procedure_id
                JOIN sources sr ON sr.id = pcf.triggering_source_id
                WHERE pcf.status = :st
                ORDER BY pcf.created_at DESC
                """
            ),
            {"st": status},
        ).mappings().all()
    return templates.TemplateResponse(
        request, "procedure_findings.html",
        _ctx(request, findings=list(rows), status=status),
    )


@sop_router.get("/brain", response_class=HTMLResponse)
def brain_search(request: Request, q: str = "", function: str | None = None) -> Response:
    if (r := auth.require_auth(request)):
        return r
    procs: list = []
    sops_list: list = []
    if q.strip():
        with session_scope() as s:
            procs = find_similar_procedures(s, q, function_slug=function, top_k=10)
            sops_list = find_similar_sops(s, q, function_slug=function, top_k=10)
    with session_scope() as s:
        functions_list = s.execute(text("SELECT slug, label FROM functions ORDER BY label")).mappings().all()
    return templates.TemplateResponse(
        request, "brain.html",
        _ctx(request, q=q, function=function, procedures=procs, sops=sops_list,
             functions=list(functions_list)),
    )
