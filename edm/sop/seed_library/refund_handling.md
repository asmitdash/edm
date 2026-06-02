---
title: Customer Refund Handling
function: support
industry: saas
---


# Customer Refund Handling — Standard Operating Procedure

## Purpose
Provide a consistent, defensible process for handling customer refund requests across channels (email, in-app, support tickets) while balancing customer satisfaction and revenue protection.

## Scope
Applies to all refund requests against paid subscriptions or one-time purchases. Excludes chargeback-driven reversals, which follow the chargeback SOP.

## Roles & Responsibilities
- **Support Agent (Tier 1):** First-touch triage, eligibility check, action on standard refunds.
- **Support Lead:** Approval for non-standard refunds.
- **Finance Operations:** Records refund event, reconciles in billing system.

## Trigger
- Customer submits a refund request via support channel.
- Auto-flagged refund eligibility from churn signal.

## Procedure Steps
1. Acknowledge request within 1 business hour.
2. Look up customer in billing system. Note plan, billing date, last-charged amount.
3. Verify request falls within stated refund window (default: 14 days).
4. Determine refund type: full, prorated, or partial.
5. If standard (within window, full refund, < $500), process directly via billing tool.
6. If non-standard, escalate per Escalation section.
7. Issue refund through billing tool. Capture transaction id.
8. Send confirmation email with refund timeline.
9. Record refund event in CRM, including reason code.
10. If churn-related, hand off to Customer Success for retention follow-up.

## Exceptions & Conditional Rules
- Refunds over $500 require Support Lead approval before processing.
- Refunds outside the 14-day window require Support Lead approval and a documented justification.
- Annual subscriptions canceled mid-cycle: prorate to nearest full month.
- Enterprise contracts: route to Finance Operations only; do not process directly.

## Escalation
- Refund > $500 OR outside refund window → Support Lead.
- Customer disputes refund denial → Support Lead, then Head of Support.
- Refund triggers chargeback risk flag → Finance Operations.

## Compliance Notes
- All refunds must be logged with reason code per finance audit policy.
- Do not communicate refund eligibility decisions verbally — written confirmation required.

## Revision History
- v1.0 — Initial draft.
