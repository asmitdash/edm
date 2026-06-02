"""Company Brain layer: cross-functional procedure extraction + retrieval.

Sits next to `edm.extract` (engineering decisions) and `edm.sop` (SOP gen).
The pipeline orchestrator routes a source to the right extractor based on its
`kind` — engineering kinds go through decisions; ops/support/legal/etc. go
through procedures.
"""
