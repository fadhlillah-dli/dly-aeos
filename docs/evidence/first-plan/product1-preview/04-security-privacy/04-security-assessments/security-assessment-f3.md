# Security Assessment — Langkah keputusan manusia dan penanda OFFICE

<!-- aeos:logical_id=04-security-assessments/security-assessment-f3 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-security-assessments/security-assessment-f3` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f3 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Scope

<!-- aeos:section=scope anchor=04-security-assessments--security-assessment-f3--scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Perubahan: fitur **Langkah keputusan manusia dan penanda OFFICE** (`f3`).

Tautan prefill (T6) dan perubahan pembacaan prefill pada formulir keputusan (T12)

## Requirements

<!-- aeos:section=requirements anchor=04-security-assessments--security-assessment-f3--requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tanpa auto-submit
- Allowlist parameter
- Binding keputusan ADR-0031 tidak berubah
- RFC-2 disetujui manusia sebelum T12

## Risks

<!-- aeos:section=risks anchor=04-security-assessments--security-assessment-f3--risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- RISK-5

## Findings

<!-- aeos:section=findings anchor=04-security-assessments--security-assessment-f3--findings content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Temuan hanya dari review/scan nyata untuk perubahan ini — tidak dikarang oleh dokumen atau model.
>
> Sumber live: AEOS security review / scanner (EvidenceArtifact tepercaya). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Mitigations

<!-- aeos:section=mitigations anchor=04-security-assessments--security-assessment-f3--mitigations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tes no-POST
- Tes parameter tidak dikenal
- Review keamanan T12 dan T8

## Residual Risk

<!-- aeos:section=residual_risk anchor=04-security-assessments--security-assessment-f3--residual-risk content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

## Approval

<!-- aeos:section=approval anchor=04-security-assessments--security-assessment-f3--approval content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

Approval security hanya melalui Decision manusia.
