# Data Inventory

<!-- aeos:logical_id=04-data-inventory renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-data-inventory` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Privacy Officer belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Data Types

<!-- aeos:section=data_types anchor=04-data-inventory--data-types content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

| Data type | Classification | Owner | Storage | Access | Retention | Disposal |
|---|---|---|---|---|---|---|
| Hasil Doctor (status, check_id, checked_at, remedy tersamarkan) | INTERNAL | UNKNOWN | Penyimpanan hasil Doctor yang ada (tidak berubah) | Baca: VIEWER+ melalui API allowlist; recheck: peran berwenang | Mengikuti kebijakan Doctor yang ada (UNKNOWN) | Mengikuti kebijakan Doctor yang ada (UNKNOWN) |
| Katalog langkah §07 | INTERNAL | UNKNOWN | File data berversi di repository | Baca oleh backend; perubahan via PR yang di-merge manusia | Riwayat git | Penghapusan via PR |
| Event audit recheck | INTERNAL | UNKNOWN | Mekanisme audit/event yang ada | Sesuai akses audit yang ada | Mengikuti kebijakan audit yang ada (UNKNOWN) | Mengikuti kebijakan audit yang ada (UNKNOWN) |

## Classification

<!-- aeos:section=classification anchor=04-data-inventory--classification content_type=STATIC -->
_Content type: STATIC_

Klasifikasi proyek (floor, record AEOS): **INTERNAL**
- Hasil Doctor (status, check_id, checked_at, remedy tersamarkan): INTERNAL
- Katalog langkah §07: INTERNAL
- Event audit recheck: INTERNAL

## Owner

<!-- aeos:section=owner anchor=04-data-inventory--owner content_type=STATIC -->
_Content type: STATIC_

UNASSIGNED (Privacy Officer belum ditetapkan)

## Storage

<!-- aeos:section=storage anchor=04-data-inventory--storage content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **Hasil Doctor (status, check_id, checked_at, remedy tersamarkan)** — Penyimpanan hasil Doctor yang ada (tidak berubah)
- **Katalog langkah §07** — File data berversi di repository
- **Event audit recheck** — Mekanisme audit/event yang ada

## Access

<!-- aeos:section=access anchor=04-data-inventory--access content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **Hasil Doctor (status, check_id, checked_at, remedy tersamarkan)** — Baca: VIEWER+ melalui API allowlist; recheck: peran berwenang
- **Katalog langkah §07** — Baca oleh backend; perubahan via PR yang di-merge manusia
- **Event audit recheck** — Sesuai akses audit yang ada

## Retention

<!-- aeos:section=retention anchor=04-data-inventory--retention content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **Hasil Doctor (status, check_id, checked_at, remedy tersamarkan)** — Mengikuti kebijakan Doctor yang ada (UNKNOWN)
- **Katalog langkah §07** — Riwayat git
- **Event audit recheck** — Mengikuti kebijakan audit yang ada (UNKNOWN)

## Disposal

<!-- aeos:section=disposal anchor=04-data-inventory--disposal content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **Hasil Doctor (status, check_id, checked_at, remedy tersamarkan)** — Mengikuti kebijakan Doctor yang ada (UNKNOWN)
- **Katalog langkah §07** — Penghapusan via PR
- **Event audit recheck** — Mengikuti kebijakan audit yang ada (UNKNOWN)
