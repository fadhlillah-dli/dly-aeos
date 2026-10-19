# Security Assessment — Halaman Penyiapan awal (read-only) berbasis Doctor

<!-- aeos:logical_id=04-security-assessments/security-assessment-f1 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-security-assessments/security-assessment-f1` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f1 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Scope

<!-- aeos:section=scope anchor=04-security-assessments--security-assessment-f1--scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Perubahan: fitur **Halaman Penyiapan awal (read-only) berbasis Doctor** (`f1`).

GET /api/setup/steps, registrasi rute (T11) dan halaman Penyiapan awal

## Requirements

<!-- aeos:section=requirements anchor=04-security-assessments--security-assessment-f1--requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Respons allowlist
- Tidak ada secret atau jalur home dalam respons/DOM
- VIEWER read-only
- Rute di belakang otorisasi yang ada

## Risks

<!-- aeos:section=risks anchor=04-security-assessments--security-assessment-f1--risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- RISK-1
- RISK-3
- RISK-6

## Findings

<!-- aeos:section=findings anchor=04-security-assessments--security-assessment-f1--findings content_type=DYNAMIC -->
_Content type: DYNAMIC_

> Temuan hanya dari review/scan nyata untuk perubahan ini — tidak dikarang oleh dokumen atau model.
>
> Sumber live: AEOS security review / scanner (EvidenceArtifact tepercaya). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.

## Mitigations

<!-- aeos:section=mitigations anchor=04-security-assessments--security-assessment-f1--mitigations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Tes pola secret dan jalur home
- Validasi katalog terhadap registri check
- Freshness eksplisit dengan ambang terdokumentasi
- Review keamanan T8

## Residual Risk

<!-- aeos:section=residual_risk anchor=04-security-assessments--security-assessment-f1--residual-risk content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

## Approval

<!-- aeos:section=approval anchor=04-security-assessments--security-assessment-f1--approval content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

Approval security hanya melalui Decision manusia.
