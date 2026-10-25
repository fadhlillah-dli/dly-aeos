# TDD — Halaman Penyiapan awal (read-only) berbasis Doctor

<!-- aeos:logical_id=03-tdds/tdd-f1 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `03-tdds/tdd-f1` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Tech Lead belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f1 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

TDD ini diperlukan karena dirujuk oleh PRD `02-requirements/prd-f1` (rencana yang disetujui):

- TDD-1

Entri rencana: `TDD-1` — Desain teknis katalog dan composer status penyiapan

## Components

<!-- aeos:section=components anchor=03-tdds--tdd-f1--components content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- SetupStepCatalog (loader + validasi IC2)
- SetupStatusComposer
- Route GET /api/setup/steps (router modul; registrasi oleh T11)
- Frontend SetupWizardPage + slot StepActions

## Data Model

<!-- aeos:section=data_model anchor=03-tdds--tdd-f1--data-model content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Step {step_key, order, title, why, unlocks[CAN_*], check_ids[], kind COMMAND|DECISION, remedy {windows_powershell, macos}, decision_link?}. Respons: {catalog_version, host_os, freshness_threshold_seconds, progress{verified,total}, steps[{...step, status, checks[{check_id,status,checked_at,freshness}]}], entry_point{show, blocked_gates[]}, office{status:'BLOCKED' (konstanta), reason}}.

## Data Flow

<!-- aeos:section=data_flow anchor=03-tdds--tdd-f1--data-flow content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Loader memvalidasi katalog saat start. Composer membaca hasil Doctor terakhir per check_id dan status gate, lalu mengagregasi status: (1) VERIFIED hanya bila semua check VERIFIED+FRESH; (2) STALE bila ada yang melewati ambang (aturan Doctor atau ADR); (3) MISSING/UNKNOWN mengikuti Doctor; (4) langkah tanpa check -> UNKNOWN. entry_point.show = ada gate unlocks yang BLOCKED atau UNKNOWN.

## API / Integration

<!-- aeos:section=api_integration anchor=03-tdds--tdd-f1--api-integration content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Antarmuka publik Doctor (baca hasil) dan capability gate; tidak memanggil layanan eksternal.

## Authn / Authz

<!-- aeos:section=authn_authz anchor=03-tdds--tdd-f1--authn-authz content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Dependensi sesi/peran yang ada; VIEWER+ untuk GET.

## Controls

<!-- aeos:section=controls anchor=03-tdds--tdd-f1--controls content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Allowlist serialisasi
- Validasi katalog saat start (gagal tertutup)
- Tes kebocoran secret dan jalur home

## Errors

<!-- aeos:section=errors anchor=03-tdds--tdd-f1--errors content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Katalog tidak valid -> endpoint 503 dengan pesan generik dan log; hasil Doctor tidak tersedia -> status UNKNOWN.

## Logs / Monitoring

<!-- aeos:section=logs_monitoring anchor=03-tdds--tdd-f1--logs-monitoring content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Log durasi dan status respons; tanpa isi remedy.

## Performance

<!-- aeos:section=performance anchor=03-tdds--tdd-f1--performance content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

p95 < 300 ms; tanpa eksekusi check.

## Deployment

<!-- aeos:section=deployment anchor=03-tdds--tdd-f1--deployment content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Bagian dari rilis controller; tanpa konfigurasi baru selain nilai ambang fallback bila diperlukan.

## Migration / Rollback

<!-- aeos:section=migration_rollback anchor=03-tdds--tdd-f1--migration-rollback content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada migrasi.

## Dependencies

<!-- aeos:section=dependencies anchor=03-tdds--tdd-f1--dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- IC1
- IC2
- Doctor
- Capability gates
- Laporan discovery T1

## Technical Risks

<!-- aeos:section=technical_risks anchor=03-tdds--tdd-f1--technical-risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Format hasil Doctor berbeda antar check
- Ambang freshness tidak terekspos (fallback ADR)

## Testing

<!-- aeos:section=testing anchor=03-tdds--tdd-f1--testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Unit composer dan entry_point dengan fixture, tes kontrak IC1/IC2, tes kebocoran, tes komponen frontend, acceptance pada data dir kosong (T9).
