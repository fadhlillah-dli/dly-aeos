# TDD — Periksa ulang per langkah

<!-- aeos:logical_id=03-tdds/tdd-f2 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `03-tdds/tdd-f2` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Tech Lead belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f2 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

TDD ini diperlukan karena dirujuk oleh PRD `02-requirements/prd-f2` (rencana yang disetujui):

- TDD-2

Entri rencana: `TDD-2` — Desain teknis recheck per langkah

## Components

<!-- aeos:section=components anchor=03-tdds--tdd-f2--components content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- SetupRecheckService
- Route POST /api/setup/steps/{step_key}/recheck
- RecheckButton (slot StepActions)

## Data Model

<!-- aeos:section=data_model anchor=03-tdds--tdd-f2--data-model content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada tabel baru; lock in-memory per step_key; event audit {type:'setup.recheck', actor, step_key, check_ids, result_status, duration_ms}; konfigurasi recheck_timeout_seconds dan rate limit dengan default dari ADR.

## Data Flow

<!-- aeos:section=data_flow anchor=03-tdds--tdd-f2--data-flow content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Validasi sesi/peran/CSRF -> step_key dari katalog -> acquire lock (409 bila terkunci) -> cek batas laju (429) -> runner Doctor(check_ids) dengan timeout -> Doctor menyimpan hasil -> composer -> respons langkah.

## API / Integration

<!-- aeos:section=api_integration anchor=03-tdds--tdd-f2--api-integration content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Runner Doctor yang ada dengan filter check_id (dukungan subset diverifikasi T1; fallback keputusan manusia).

## Authn / Authz

<!-- aeos:section=authn_authz anchor=03-tdds--tdd-f2--authn-authz content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Hanya peran berwenang sesuai IC1; VIEWER 403; tanpa sesi 401; CSRF wajib.

## Controls

<!-- aeos:section=controls anchor=03-tdds--tdd-f2--controls content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Lock per langkah
- Batas laju per sesi (konfigurasi)
- Timeout (konfigurasi)
- Audit

## Errors

<!-- aeos:section=errors anchor=03-tdds--tdd-f2--errors content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Timeout -> status UNKNOWN + pesan; error runner -> 500 generik tanpa detail sensitif.

## Logs / Monitoring

<!-- aeos:section=logs_monitoring anchor=03-tdds--tdd-f2--logs-monitoring content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Log hitungan/durasi recheck per step_key; metrik/alert hanya usulan.

## Performance

<!-- aeos:section=performance anchor=03-tdds--tdd-f2--performance content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Dibatasi oleh check; timeout default usulan 60 detik.

## Deployment

<!-- aeos:section=deployment anchor=03-tdds--tdd-f2--deployment content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Bagian dari rilis controller.

## Migration / Rollback

<!-- aeos:section=migration_rollback anchor=03-tdds--tdd-f2--migration-rollback content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada migrasi.

## Dependencies

<!-- aeos:section=dependencies anchor=03-tdds--tdd-f2--dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- IC1
- Doctor runner
- Mekanisme audit
- Laporan discovery T1 (model proses, peran)

## Technical Risks

<!-- aeos:section=technical_risks anchor=03-tdds--tdd-f2--technical-risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Runner tidak mendukung subset (RISK-7)
- Lock in-memory hanya valid untuk proses tunggal; model proses dikonfirmasi T1

## Testing

<!-- aeos:section=testing anchor=03-tdds--tdd-f2--testing content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tes spy subset dengan runner tiruan, negatif 401/403/404/409/429/CSRF, 409 deterministik dengan event/semaphore, timeout, audit; tes komponen tombol.
