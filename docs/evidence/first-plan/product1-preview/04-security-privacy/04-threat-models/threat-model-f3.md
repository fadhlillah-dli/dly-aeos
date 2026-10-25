# Threat Model — Langkah keputusan manusia dan penanda OFFICE

<!-- aeos:logical_id=04-threat-models/threat-model-f3 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-threat-models/threat-model-f3` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f3 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Assets

<!-- aeos:section=assets anchor=04-threat-models--threat-model-f3--assets content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Integritas antrean keputusan

## Actors

<!-- aeos:section=actors anchor=04-threat-models--threat-model-f3--actors content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Pemilik
- Pembuat tautan tidak tepercaya

## Trust Boundaries

<!-- aeos:section=trust_boundaries anchor=04-threat-models--threat-model-f3--trust-boundaries content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- URL/query parameter <-> formulir keputusan

## Entry Points

<!-- aeos:section=entry_points anchor=04-threat-models--threat-model-f3--entry-points content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Query parameter prefill

## Threats

<!-- aeos:section=threats anchor=04-threat-models--threat-model-f3--threats content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F3-TH-1** — Tampering / social engineering

## Attack Scenarios

<!-- aeos:section=attack_scenarios anchor=04-threat-models--threat-model-f3--attack-scenarios content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F3-TH-1** — Tautan prefill berisi target berbahaya untuk write enablement.

## Mitigations

<!-- aeos:section=mitigations anchor=04-threat-models--threat-model-f3--mitigations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F3-TH-1** — Tinjauan manual, allowlist, tanpa auto-submit.
