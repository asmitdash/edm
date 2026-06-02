---
title: Production Incident Response
function: engineering
industry: saas
---

# Production Incident Response — Standard Operating Procedure

## Purpose
Define the engineering on-call response to user-impacting production incidents to minimize time-to-mitigation and ensure post-incident learning.

## Scope
Applies to all SEV-1 / SEV-2 incidents on production services. Internal-tool degradations follow a separate SOP.

## Roles & Responsibilities
- **Primary On-call:** First responder, runs the incident.
- **Secondary On-call:** Pages in if primary is unreachable in 5 min.
- **Incident Commander:** Coordinates if SEV-1.
- **Comms Lead:** Customer-facing status updates.

## Trigger
- Pager fires from monitoring (latency, error rate, availability SLO breach).
- Customer-reported outage confirmed by support.
- Internal user reports loss of critical functionality.

## Procedure Steps
1. Acknowledge page within 5 minutes.
2. Open incident channel and incident doc.
3. Classify severity: SEV-1 (multi-customer impact), SEV-2 (single-customer), SEV-3 (degradation).
4. If SEV-1, page Incident Commander and Comms Lead.
5. Identify probable cause (recent deploy, dependency, infra).
6. Apply mitigation (rollback, feature flag off, scale up).
7. Verify mitigation in monitoring dashboards.
8. Communicate status update every 30 minutes (SEV-1) or 60 minutes (SEV-2).
9. Once mitigated, post all-clear.
10. Schedule post-mortem within 5 business days.

## Exceptions & Conditional Rules
- Data-loss incidents are always SEV-1 regardless of customer count.
- Security incidents skip this SOP and follow the security IR SOP.
- Outside business hours, on-call may proceed with rollback without IC approval.

## Escalation
- SEV-1 lasting > 30 min without mitigation → wake VP Engineering.
- Customer escalation from CS Lead → loop in Comms Lead.

## Compliance Notes
- Customer data exposure incidents trigger the privacy-incident SOP.
- All incident docs retained for 7 years per audit policy.

## Revision History
- v1.0 — Initial draft.
