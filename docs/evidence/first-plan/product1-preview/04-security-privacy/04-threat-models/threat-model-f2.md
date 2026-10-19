# Threat Model — Periksa ulang per langkah

<!-- aeos:logical_id=04-threat-models/threat-model-f2 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-threat-models/threat-model-f2` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). (Security Lead) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | requirement: f2 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Assets

<!-- aeos:section=assets anchor=04-threat-models--threat-model-f2--assets content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Eksekusi check
- Kuota eksternal

## Actors

<!-- aeos:section=actors anchor=04-threat-models--threat-model-f2--actors content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Pemilik
- VIEWER
- Situs pihak ketiga (CSRF)

## Trust Boundaries

<!-- aeos:section=trust_boundaries anchor=04-threat-models--threat-model-f2--trust-boundaries content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Browser <-> API
- API <-> Doctor runner

## Entry Points

<!-- aeos:section=entry_points anchor=04-threat-models--threat-model-f2--entry-points content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- POST recheck

## Threats

<!-- aeos:section=threats anchor=04-threat-models--threat-model-f2--threats content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F2-TH-1** — Elevation of privilege
- **F2-TH-2** — CSRF
- **F2-TH-3** — Injection

## Attack Scenarios

<!-- aeos:section=attack_scenarios anchor=04-threat-models--threat-model-f2--attack-scenarios content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F2-TH-1** — VIEWER memicu recheck.
- **F2-TH-2** — Situs lain memicu recheck.
- **F2-TH-3** — step_key dipakai memilih check arbitrer.

## Mitigations

<!-- aeos:section=mitigations anchor=04-threat-models--threat-model-f2--mitigations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- **F2-TH-1** — Gate peran + tes 403.
- **F2-TH-2** — Proteksi CSRF yang ada + tes F2-AC6.
- **F2-TH-3** — check_ids hanya dari katalog.
