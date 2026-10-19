# Security Assessment — Periksa ulang per langkah

<!-- aeos:logical_id=04-security-assessments/security-assessment-f2 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-security-assessments/security-assessment-f2` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f2 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Scope

<!-- aeos:section=scope anchor=04-security-assessments--security-assessment-f2--scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Perubahan: fitur **Periksa ulang per langkah** (`f2`).

POST /api/setup/steps/{step_key}/recheck

## Requirements

<!-- aeos:section=requirements anchor=04-security-assessments--security-assessment-f2--requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Gate peran server-side dengan peran persis dari IC1
- CSRF
- Validasi step_key
- Lock dan batas laju (konfigurasi)
- Audit

## Risks

<!-- aeos:section=risks anchor=04-security-assessments--security-assessment-f2--risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- RISK-2
- RISK-4
- RISK-7

## Findings

<!-- aeos:section=findings anchor=04-security-assessments--security-assessment-f2--findings content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Temuan hanya dari review/scan nyata untuk perubahan ini — tidak dikarang oleh dokumen atau model.
>
> Sumber live: AEOS security review / scanner (EvidenceArtifact tepercaya). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Mitigations

<!-- aeos:section=mitigations anchor=04-security-assessments--security-assessment-f2--mitigations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tes negatif 401/403/404/409/429/CSRF
- Tes 409 deterministik
- Review keamanan T4 dan T8

## Residual Risk

<!-- aeos:section=residual_risk anchor=04-security-assessments--security-assessment-f2--residual-risk content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

## Approval

<!-- aeos:section=approval anchor=04-security-assessments--security-assessment-f2--approval content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

Approval security hanya melalui Decision manusia.
