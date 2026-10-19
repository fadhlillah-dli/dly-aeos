# Operational Overview

<!-- aeos:logical_id=08-operational-overview renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `08-operational-overview` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Operations Owner belum ditetapkan) |
| Status (saat render) | DRAFT — Usulan operasional dari rencana yang disetujui; status APPROVED hanya setelah evidence production validation (§28). |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

_Usulan dari rencana (AI_MAINTAINED proposal); bukan bukti operasi berjalan._

## Production Architecture

<!-- aeos:section=production_architecture anchor=08-operational-overview--production-architecture content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Fitur berjalan di dalam proses Control Center yang ada; tidak ada layanan baru. Dampak operasional utama adalah eksekusi check melalui recheck. Monitoring, alert dan SLO di bawah adalah usulan dan tidak dibangun di slice ini.

## Ownership

<!-- aeos:section=ownership anchor=08-operational-overview--ownership content_type=STATIC -->
_Content type: STATIC_

UNASSIGNED (Operations Owner belum ditetapkan)

## Dependencies

<!-- aeos:section=dependencies anchor=08-operational-overview--dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Readiness Doctor
- Capability gates
- OpenBao (melalui check)
- Sistem audit/event yang ada
- Formulir keputusan yang ada

## Support

<!-- aeos:section=support anchor=08-operational-overview--support content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

_Documentation pending — belum ada di PlanVersion yang disetujui; tidak dikarang oleh AEOS._

## Criticality

<!-- aeos:section=criticality anchor=08-operational-overview--criticality content_type=STATIC -->
_Content type: STATIC_

UNKNOWN (belum ditetapkan di rencana)

## SLI / SLO / SLA

<!-- aeos:section=sli_slo_sla anchor=08-operational-overview--sli-slo-sla content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **Ketersediaan GET /api/setup/steps (rasio respons non-5xx)** — objective (usulan): 99% per 7 hari pada instalasi lokal (usulan; pengukuran belum dibangun); alasan: Halaman ini adalah jalur utama bootstrap; target sejalan dengan pendekatan ADR-0092.
- **Latensi p95 GET /api/setup/steps** — objective (usulan): < 300 ms; alasan: Endpoint hanya membaca hasil tersimpan; diuji di TP1-S9.
- **Rasio recheck yang berakhir timeout** — objective (usulan): < 5% per 7 hari (usulan; pengukuran belum dibangun); alasan: Mendeteksi check yang bermasalah atau ketergantungan eksternal.
