# Security Overview

<!-- aeos:logical_id=04-security-overview renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-security-overview` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Ownership

<!-- aeos:section=ownership anchor=04-security-overview--ownership content_type=STATIC -->
_Content type: STATIC_

UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead / ownership menurut rencana)

## Authn / Authz

<!-- aeos:section=authn_authz anchor=04-security-overview--authn-authz content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Memakai sesi dan model peran Control Center yang ada (ADR-0004). GET /api/setup/steps untuk VIEWER ke atas. POST recheck hanya untuk peran berwenang; nama peran persis diverifikasi T1 dan ditulis di IC1. VIEWER 403, tanpa sesi 401.

## Encryption

<!-- aeos:section=encryption anchor=04-security-overview--encryption content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Tidak ada penyimpanan data baru; mengikuti transport dan penyimpanan yang ada. Tidak ada secret yang dibaca oleh wizard.

## Secrets

<!-- aeos:section=secrets anchor=04-security-overview--secrets content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Wizard tidak menerima, membaca, merender atau mengirim secret. Perintah remedy yang membutuhkan secret bersifat interaktif di terminal (mis. 'aeos secrets set personal/github') tanpa nilai. Penanda [REDACTED] dari remedy dipertahankan.

## Logs / Monitoring

<!-- aeos:section=logs_monitoring anchor=04-security-overview--logs-monitoring content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Event audit untuk setiap recheck (aktor, step_key, check_ids, status hasil, durasi) tanpa detail sensitif; log error tanpa isi remedy mentah.

## Controls

<!-- aeos:section=controls anchor=04-security-overview--controls content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Allowlist field pada respons API
- Validasi step_key terhadap katalog; check_id tidak diterima dari klien
- CSRF/same-origin untuk POST
- Lock per langkah dan batas laju recheck (nilai konfigurasi)
- Tidak ada auto-submit keputusan; prefill dengan allowlist field (T12)
- Tes otomatis pemindaian pola secret dan pola jalur home pada respons dan DOM
