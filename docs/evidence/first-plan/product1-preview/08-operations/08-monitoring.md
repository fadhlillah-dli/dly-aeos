# Monitoring

<!-- aeos:logical_id=08-monitoring renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `08-monitoring` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Operations Owner belum ditetapkan) |
| Status (saat render) | DRAFT — Usulan operasional dari rencana yang disetujui; status APPROVED hanya setelah evidence production validation (§28). |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

_Usulan dari rencana (AI_MAINTAINED proposal); bukan bukti operasi berjalan._

## Metrics

<!-- aeos:section=metrics anchor=08-monitoring--metrics content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Usulan (tidak dibangun di slice ini): hitungan dan durasi recheck per step_key
- Usulan (tidak dibangun di slice ini): rasio 4xx/5xx endpoint setup
- Usulan (tidak dibangun di slice ini): jumlah langkah STALE/UNKNOWN

## Dashboards

<!-- aeos:section=dashboards anchor=08-monitoring--dashboards content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Dashboard dirujuk setelah production validation.
>
> Sumber live: Sistem monitoring produksi. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Coverage

<!-- aeos:section=coverage anchor=08-monitoring--coverage content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Cakupan monitoring dibaca dari sistem monitoring.
>
> Sumber live: Sistem monitoring produksi. Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Thresholds

<!-- aeos:section=thresholds anchor=08-monitoring--thresholds content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **Ketersediaan GET /api/setup/steps (rasio respons non-5xx)** — objective (usulan): 99% per 7 hari pada instalasi lokal (usulan; pengukuran belum dibangun); alasan: Halaman ini adalah jalur utama bootstrap; target sejalan dengan pendekatan ADR-0092.
- **Latensi p95 GET /api/setup/steps** — objective (usulan): < 300 ms; alasan: Endpoint hanya membaca hasil tersimpan; diuji di TP1-S9.
- **Rasio recheck yang berakhir timeout** — objective (usulan): < 5% per 7 hari (usulan; pengukuran belum dibangun); alasan: Mendeteksi check yang bermasalah atau ketergantungan eksternal.
