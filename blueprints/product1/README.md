# PRODUCT 1 — SOFTWARE DEVELOPMENT & ENGINEERING (blueprint)

Generic, tenant-free blueprint for the Product 1 documentation set of every project managed by
DLY AEOS (master §27, §28, U-08). The single code source of the structure is
`backend/aeos/docs_engine/product1.py`; `aeos docs structure` prints it and
`GET /api/docs/product1-structure` serves it to the Control Center.

* Title: `PRODUCT 1 (<Nama Produk>) — SOFTWARE DEVELOPMENT & ENGINEERING`; the outer folder is the
  project name.
* Folders only where a node holds several documents (02 Requirements, 03 RFCs/TDDs, 04 assessment
  collections, 06 Testing, 07 Release, 08 Operations, 09 Incidents & Problems, 10 Changes,
  11 Improvements, Templates). Product Home, Product Overview, Development and Retirement are single
  documents with sections — never a subfolder per heading.
* Test Strategy is a section inside the Test Plan; Release Notes is a section inside the Release Plan.
* Every section has a content type: `STATIC`, `DYNAMIC` (reference the live source, never copy
  status), `AI_MAINTAINED` (narrative from the approved plan) or `HUMAN_GOVERNED` (decisions, risk
  acceptance, approval, sign-off — never filled by AEOS).
* Future nodes are `NOT STARTED` / `NOT APPLICABLE` with reason and owner. PT/VA/UAT/restore-test
  results and approvals are never fabricated.

`templates/` holds the twelve generic templates required by §27: PRD, RFC, TDD, Risk Assessment,
Security Assessment, Threat Model, Privacy Assessment / DPIA, Test Plan, Release Plan, Runbook,
Post-Mortem, Retirement & Decommission. They are generated from the spec (a unit test keeps them in
sync) and contain no tenant data, credentials or Office content.
