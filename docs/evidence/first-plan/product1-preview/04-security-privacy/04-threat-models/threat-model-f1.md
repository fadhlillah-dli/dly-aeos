# Threat Model — Halaman Penyiapan awal (read-only) berbasis Doctor

<!-- aeos:logical_id=04-threat-models/threat-model-f1 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-threat-models/threat-model-f1` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f1 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Assets

<!-- aeos:section=assets anchor=04-threat-models--threat-model-f1--assets content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Status readiness
- Secret (tidak langsung)
- Nama pengguna OS dalam jalur

## Actors

<!-- aeos:section=actors anchor=04-threat-models--threat-model-f1--actors content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Pemilik
- VIEWER

## Trust Boundaries

<!-- aeos:section=trust_boundaries anchor=04-threat-models--threat-model-f1--trust-boundaries content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Browser <-> API

## Entry Points

<!-- aeos:section=entry_points anchor=04-threat-models--threat-model-f1--entry-points content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- GET /api/setup/steps

## Threats

<!-- aeos:section=threats anchor=04-threat-models--threat-model-f1--threats content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F1-TH-1** — Information disclosure
- **F1-TH-2** — Information disclosure (data pribadi)

## Attack Scenarios

<!-- aeos:section=attack_scenarios anchor=04-threat-models--threat-model-f1--attack-scenarios content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F1-TH-1** — Remedy/detail Doctor memuat fragmen secret yang dirender.
- **F1-TH-2** — Remedy memuat jalur home berisi nama pengguna OS yang terlihat VIEWER.

## Mitigations

<!-- aeos:section=mitigations anchor=04-threat-models--threat-model-f1--mitigations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F1-TH-1** — Allowlist dan tes kebocoran.
- **F1-TH-2** — Pemeriksaan T1, template tanpa jalur pengguna, tes pola jalur home.
