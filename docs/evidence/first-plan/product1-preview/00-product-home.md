# 00. Product Home — PRODUCT 1 (DLY AEOS) — SOFTWARE DEVELOPMENT & ENGINEERING

<!-- aeos:logical_id=00-product-home renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `00-product-home` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Product Owner belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: 02-requirements/prd-f1, 02-requirements/prd-f2, 02-requirements/prd-f3, 02-requirements/prd-f4 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

**PRODUCT 1 (DLY AEOS) — SOFTWARE DEVELOPMENT & ENGINEERING**

## Identity

<!-- aeos:section=identity anchor=00-product-home--identity content_type=STATIC -->
_Content type: STATIC_

| Atribut | Nilai |
|---|---|
| Nama produk | PRODUCT 1 (DLY AEOS) — SOFTWARE DEVELOPMENT & ENGINEERING |
| Project ID | `b0e17ee8-cb44-4c43-8a12-10b4d5c43efe` |
| Project key | `dly-aeos` |
| Domain | PERSONAL (dipilih manusia; tidak diubah oleh model) |
| Jenis proyek | EXISTING — proyek platform DLY AEOS (self-build) |
| Lifecycle | ACTIVE |

Platform DLY AEOS (self-build §09). Slice 1 upgrade: halaman 'Penyiapan awal' di Control Center yang memandu pemilik melalui urutan bootstrap §07 hanya dengan Doctor check, capability gate dan endpoint keputusan yang sudah ada.

## Product Owner / Engineering Lead / Tech Lead

<!-- aeos:section=ownership anchor=00-product-home--ownership content_type=STATIC -->
_Content type: STATIC_

| Peran | Nama |
|---|---|
| Product Owner | belum ditetapkan |
| Engineering Lead | belum ditetapkan |
| Tech Lead | belum ditetapkan |

## Repository / Issue Tracker / Service

<!-- aeos:section=repository anchor=00-product-home--repository content_type=STATIC -->
_Content type: STATIC_

| Jenis | Referensi |
|---|---|
| Repository | fadhlillah-dli/dly-aeos (GITHUB) |
| Issue tracker | GitHub Issues (fadhlillah-dli/dly-aeos) |
| Dokumentasi resmi | Google Docs (Google Drive folder proyek) |
| Service | DLY AEOS Control Center (backend Python + frontend TypeScript) |
| Repository (referensi di rencana) | GITHUB:fadhlillah-dli/dly-aeos (verifikasi UNVERIFIED; branch inventaris integration/wave3 @ 959bed284ac492b49c74c4dea18613eed2727b70) |
| Issue tracker (referensi di rencana) | GitHub Issues fadhlillah-dli/dly-aeos (CONFIGURED, belum pernah diverifikasi; kredensial UNKNOWN) |

## Criticality / Data Classification

<!-- aeos:section=classification anchor=00-product-home--classification content_type=STATIC -->
_Content type: STATIC_

| Atribut | Nilai |
|---|---|
| Criticality | UNKNOWN (belum ditetapkan di rencana) |
| Data classification (record proyek, floor) | INTERNAL |
| Data classification (usulan rencana) | INTERNAL — record proyek tetap berlaku sebagai batas bawah |

## Stack

<!-- aeos:section=stack anchor=00-product-home--stack content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Python (backend/aeos, pytest)
- Alembic migrations (backend/aeos/migrations)
- TypeScript frontend (frontend/, Vitest, @testing-library/react)
- OpenBao (secret provider, ADR-0042/0043)
- GitHub Actions CI (.github/workflows/ci.yml)
- Sandbox container (sandbox/Dockerfile)

## Quick Links

<!-- aeos:section=quick_links anchor=00-product-home--quick-links content_type=DYNAMIC -->
_Content type: DYNAMIC_

- 00. Product Home: `00-product-home`
- 01. Product Overview: `01-product-overview`
- Architecture Overview: `03-architecture-overview`
- Security Overview: `04-security-overview`
- Data Inventory: `04-data-inventory`
- Security & Privacy Decisions: `04-security-privacy-decisions`
- 05. Development: `05-development`
- Operational Overview: `08-operational-overview`
- Monitoring: `08-monitoring`
- Logs: `08-logs`
- Alerts: `08-alerts`
- Service Level Objectives: `08-slo`
- Backup & Restore: `08-backup-restore`
- Maintenance: `08-maintenance`
- 12. Retirement: `12-retirement`
- Repository: https://github.com/fadhlillah-dli/dly-aeos

**Tautan dari rencana**

- README.md
- docs/architecture/DLY_AEOS_Master_Architecture_Final.md
- docs/SETUP_WINDOWS.md
- docs/SETUP_MACOS.md
- docs/OPERATIONS.md
- docs/decisions/ADR-0011-readiness-doctor-capability-gates.md
- docs/decisions/ADR-0032-single-human-decision-queue.md
- docs/runbooks/README.md

Tautan eksternal (Google Docs/Confluence/issue) ditambahkan oleh publisher sebagai ExternalReference setelah publikasi.

## Current Development

<!-- aeos:section=current_development anchor=00-product-home--current-development content_type=DYNAMIC -->
_Content type: DYNAMIC_

Catatan rencana saat disetujui (bukan status live): Slice 1: first-run setup wizard (Penyiapan awal). Status: proposal rencana versi 2, belum disetujui. Langkah pertama: T1 (discovery + kontrak) dengan checkpoint manusia.

> Pekerjaan berjalan (issue/work item) diproyeksikan dari tracker; status Done di tracker tidak membuka dependency runtime tanpa bukti penerimaan.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Active Epics / Features / Release

<!-- aeos:section=active_work anchor=00-product-home--active-work content_type=DYNAMIC -->
_Content type: DYNAMIC_

| Feature | Judul | Prioritas | Milestone | Aktif (rencana) |
|---|---|---|---|---|
| `f1` | Halaman Penyiapan awal (read-only) berbasis Doctor | P0 | M1 | ya |
| `f2` | Periksa ulang per langkah | P0 | M2 | ya |
| `f3` | Langkah keputusan manusia dan penanda OFFICE | P1 | M2 | ya |
| `f4` | Titik masuk dari Ringkasan | P1 | M3 | ya |

| Release | Judul | Release Plan |
|---|---|---|
| UNKNOWN (usulan: minor bump dari versi controller saat ini) | Slice 1: Penyiapan awal | `07-release-deployment/release-plan-unknown-usulan-minor-bump-dari-versi-con` |

> Status epic/fitur/release dibaca dari tracker dan Release Plans.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Open Risks / Security Findings

<!-- aeos:section=open_risks anchor=00-product-home--open-risks content_type=DYNAMIC -->
_Content type: DYNAMIC_

Rencana yang disetujui mencatat 7 risiko awal (`RISK-1`, `RISK-2`, `RISK-3`, `RISK-4`, `RISK-5`, `RISK-6`, `RISK-7`) — lihat Initial Risk Assessment.

Risiko terbuka menurut rencana: `RISK-1`, `RISK-2`, `RISK-3`, `RISK-4`, `RISK-5`, `RISK-6`, `RISK-7`

> Temuan security terbuka hanya dari review/evidence nyata.
>
> Sumber live: AEOS verifier/security review dan tracker. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## SDLC Lifecycle

<!-- aeos:section=sdlc_lifecycle anchor=00-product-home--sdlc-lifecycle content_type=STATIC -->
_Content type: STATIC_

Lifecycle proyek: **ACTIVE**.

Catatan rencana: Proposal rencana -> persetujuan manusia -> run baseline oleh verifier tepercaya -> discovery + kontrak (T1) -> checkpoint manusia (laporan discovery, IC1-IC3, revisi allowed_paths T11) -> implementasi per task di branch kandidat -> verifier tepercaya -> PR GitHub -> merge oleh manusia -> rilis oleh manusia

| Tahap SDLC (§28) | Dokumen |
|---|---|
| PROJECT_CREATION | Product Home, Overview, initial PRD, Architecture Overview, initial risk/security, development baseline, skeleton lifecycle |
| FEATURE_APPROVAL | Detail PRD/RFC/TDD bila diperlukan, API/schema/design contract, threat/assessment, Test Plan dan AC |
| IMPLEMENTATION_PR | Delta kontrak, keputusan dan perilaku yang berubah; machine references dan test evidence; tidak menulis ulang seluruh handbook |
| RELEASE_CANDIDATE | Snapshot konsisten, Release Plan termasuk Release Notes, migration/rollback/readiness, runbook sesuai scope |
| PRODUCTION_VALIDATION | Referensi deployment, post-release checks, monitoring, operational ownership; status hanya berdasarkan evidence |
| INCIDENT | Timeline dan evidence, analisis/keputusan, corrective action; kegagalan menjadi regression/eval case |
| RETIREMENT | Plan closure dan evidence eksekusi ketika lifecycle benar-benar retirement |

## Governance Links

<!-- aeos:section=governance_links anchor=00-product-home--governance-links content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

Referensi: AEOS Control Center → Keputusan (Decision registry) untuk approval rencana, publikasi, merge dan risk acceptance.

Tautan governance yang diusulkan rencana (DATA, bukan approval):

- docs/decisions/ADR-0011-readiness-doctor-capability-gates.md
- docs/decisions/ADR-0031-decision-binding-and-external-write-interlock.md
- docs/decisions/ADR-0032-single-human-decision-queue.md
- docs/decisions/ADR-0004-first-admin-bootstrap-hardening.md
- docs/decisions/ADR-0051-worker-sandbox-requirement.md
- docs/decisions/ADR-0090-backup-restore-test-readiness.md
- contracts/api/routes.json
- contracts/security/asvs-selection.yaml
