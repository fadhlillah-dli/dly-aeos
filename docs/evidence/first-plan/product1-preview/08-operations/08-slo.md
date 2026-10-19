# Service Level Objectives

<!-- aeos:logical_id=08-slo renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `08-slo` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Operations Owner belum ditetapkan) |
| Status (saat render) | DRAFT — Usulan operasional dari rencana yang disetujui; status APPROVED hanya setelah evidence production validation (§28). |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

_Usulan dari rencana (AI_MAINTAINED proposal); bukan bukti operasi berjalan._

## Availability Objective

<!-- aeos:section=availability anchor=08-slo--availability content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Indikator lain yang diusulkan**

- **Ketersediaan GET /api/setup/steps (rasio respons non-5xx)** — objective (usulan): 99% per 7 hari pada instalasi lokal (usulan; pengukuran belum dibangun); alasan: Halaman ini adalah jalur utama bootstrap; target sejalan dengan pendekatan ADR-0092.
- **Rasio recheck yang berakhir timeout** — objective (usulan): < 5% per 7 hari (usulan; pengukuran belum dibangun); alasan: Mendeteksi check yang bermasalah atau ketergantungan eksternal.

## Latency Objective

<!-- aeos:section=latency anchor=08-slo--latency content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **Latensi p95 GET /api/setup/steps** — objective (usulan): < 300 ms; alasan: Endpoint hanya membaca hasil tersimpan; diuji di TP1-S9.

## Error Objective

<!-- aeos:section=error_rate anchor=08-slo--error-rate content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._

## Error Budget / Measurement

<!-- aeos:section=error_budget anchor=08-slo--error-budget content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Error budget dan pengukuran hanya dari data SLI nyata.
>
> Sumber live: AEOS Operations (SLI dari evidence runtime). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
