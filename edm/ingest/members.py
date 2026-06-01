"""Fetch GitHub org members and snapshot them into `github_members`.
Also computes per-member contribution stats from `sources` for the dashboard
and PDF report."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from edm.db import session_scope
from edm.ingest import github_oauth


@dataclass
class MemberStat:
    github_login: str
    display_name: str | None
    avatar_url: str | None
    profile_url: str | None
    role_in_org: str | None
    n_prs: int
    n_decisions: int
    n_findings: int          # contradictions involving this member's decisions
    last_activity_at: str | None


def refresh_members(token: str, org: str) -> int:
    members = github_oauth.list_org_members(token, org)
    with session_scope() as s:
        for m in members:
            s.execute(
                text(
                    """
                    INSERT INTO github_members
                      (org_login, github_login, display_name, avatar_url, role_in_org, profile_url)
                    VALUES (:org, :gl, :dn, :av, :ro, :pu)
                    ON CONFLICT (org_login, github_login) DO UPDATE
                      SET display_name=EXCLUDED.display_name,
                          avatar_url=EXCLUDED.avatar_url,
                          role_in_org=EXCLUDED.role_in_org,
                          profile_url=EXCLUDED.profile_url,
                          last_seen_at=now()
                    """
                ),
                {
                    "org": org,
                    "gl": m["login"],
                    "dn": m.get("name"),
                    "av": m.get("avatar_url"),
                    "ro": m.get("role"),
                    "pu": m.get("html_url"),
                },
            )
    return len(members)


def member_stats(org: str) -> list[MemberStat]:
    """Combine github_members with PR/decision/finding counts."""
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                WITH src_counts AS (
                  SELECT s.author AS gh_login,
                         COUNT(*) FILTER (WHERE s.kind='pr')        AS n_prs,
                         MAX(s.occurred_at)                         AS last_activity_at
                  FROM sources s
                  WHERE s.author IS NOT NULL
                  GROUP BY s.author
                ),
                decision_counts AS (
                  SELECT s.author AS gh_login, COUNT(d.id) AS n_decisions
                  FROM decisions d
                  JOIN extractions e ON e.id = d.extraction_id
                  JOIN sources s ON s.id = e.source_id
                  WHERE s.author IS NOT NULL
                  GROUP BY s.author
                ),
                finding_counts AS (
                  SELECT s.author AS gh_login, COUNT(cf.id) AS n_findings
                  FROM conflict_findings cf
                  JOIN decisions d ON d.id = cf.conflicting_decision_id
                  JOIN extractions e ON e.id = d.extraction_id
                  JOIN sources s ON s.id = e.source_id
                  WHERE s.author IS NOT NULL
                  GROUP BY s.author
                )
                SELECT gm.github_login, gm.display_name, gm.avatar_url, gm.profile_url, gm.role_in_org,
                       COALESCE(sc.n_prs, 0)      AS n_prs,
                       COALESCE(dc.n_decisions, 0) AS n_decisions,
                       COALESCE(fc.n_findings, 0)  AS n_findings,
                       sc.last_activity_at
                FROM github_members gm
                LEFT JOIN src_counts sc      ON sc.gh_login = gm.github_login
                LEFT JOIN decision_counts dc ON dc.gh_login = gm.github_login
                LEFT JOIN finding_counts fc  ON fc.gh_login = gm.github_login
                WHERE gm.org_login = :org
                ORDER BY n_decisions DESC, n_prs DESC, gm.github_login ASC
                """
            ),
            {"org": org},
        ).mappings().all()
    return [
        MemberStat(
            github_login=r["github_login"],
            display_name=r["display_name"],
            avatar_url=r["avatar_url"],
            profile_url=r["profile_url"],
            role_in_org=r["role_in_org"],
            n_prs=r["n_prs"],
            n_decisions=r["n_decisions"],
            n_findings=r["n_findings"],
            last_activity_at=r["last_activity_at"].isoformat() if r["last_activity_at"] else None,
        )
        for r in rows
    ]
