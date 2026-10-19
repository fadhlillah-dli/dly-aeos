# PRD — Langkah keputusan manusia dan penanda OFFICE

<!-- aeos:logical_id=02-requirements/prd-f3 renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `02-requirements/prd-f3` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Product Owner belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | related: 03-rfcs/rfc-f3, 06-testing/test-plan-tp1, 06-testing/test-plan-tp2; requirement: f3 |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=02-requirements--prd-f3--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Langkah keputusan (aktivasi rute worker, pengecualian process-sandbox, pengaktifan write) menautkan ke formulir keputusan yang ada dengan bentuk target terisi. OFFICE ditampilkan BLOCKED; pada slice ini status tersebut adalah konstanta.

## Problem

<!-- aeos:section=problem anchor=02-requirements--prd-f3--problem content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Pemilik tidak tahu formulir keputusan mana dan target apa yang perlu diisi untuk membuka gate.

## Background

<!-- aeos:section=background anchor=02-requirements--prd-f3--background content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Keputusan dicatat melalui antrean keputusan tunggal (ADR-0032) dengan binding keputusan (ADR-0031). Wizard tidak boleh mencatat keputusan. Belum ada sumber status 'otorisasi perusahaan tercatat' yang diketahui; T1 mengonfirmasi.

## Goals / Non-goals

<!-- aeos:section=goals_non_goals anchor=02-requirements--prd-f3--goals-non-goals content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Goals**

- Mengurangi kesalahan bentuk target
- Mempertahankan keputusan sebagai tindakan manusia eksplisit

**Non-goals**

- Mencatat, menyetujui atau mengirim keputusan dari wizard
- Langkah setup OFFICE
- Membaca atau menampilkan jalur positif otorisasi perusahaan

## Business Requirements

<!-- aeos:section=business_requirements anchor=02-requirements--prd-f3--business-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Keputusan hanya tercatat setelah manusia meninjau dan mengirim formulir yang ada

## Functional Requirements

<!-- aeos:section=functional_requirements anchor=02-requirements--prd-f3--functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Requirement (rencana):** Tautan ke formulir keputusan yang ada dengan target terisi tanpa mencatat keputusan; OFFICE tampil BLOCKED (konstanta pada slice ini).

1. FR1: Katalog langkah DECISION memuat decision_link {form, target} yang mengikuti kontrak prefill IC3.
2. FR2: Tautan membuka formulir keputusan yang ada dengan field terisi; formulir tetap menampilkan semua field untuk ditinjau dan memerlukan submit manual.
3. FR3: Nilai prefill divalidasi formulir terhadap allowlist field; parameter tidak dikenal diabaikan; tidak ada auto-submit (T12).
4. FR4: Status langkah DECISION tetap berasal dari Doctor/capability gate, bukan dari klik tautan.
5. FR5: Bagian OFFICE menampilkan office.status = BLOCKED (konstanta di IC1 pada slice ini) dengan alasan 'otorisasi perusahaan belum tercatat' tanpa langkah aksi. Jalur positif berada di luar slice ini.
6. FR6: Tautan keputusan tidak dirender untuk VIEWER.

## Non-functional Requirements

<!-- aeos:section=non_functional_requirements anchor=02-requirements--prd-f3--non-functional-requirements content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- NFR1: Tes membuktikan tidak ada request POST ke endpoint keputusan saat halaman wizard dibuka atau tautan diklik.
- NFR2: Parameter prefill tidak pernah memuat secret (divalidasi tes skema IC3).
- NFR3: Tautan stabil terhadap perubahan urutan langkah (berbasis step_key).
- NFR4: Logika submit dan binding keputusan pada formulir tidak berubah (tes decisions yang ada tetap lulus).

## User Stories

<!-- aeos:section=user_stories anchor=02-requirements--prd-f3--user-stories content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Sebagai pemilik, saya ingin langsung membuka formulir aktivasi rute worker dengan target yang benar, lalu memutuskan sendiri.

## Acceptance Criteria

<!-- aeos:section=acceptance_criteria anchor=02-requirements--prd-f3--acceptance-criteria content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

1. AC: F3-AC1
2. AC: F3-AC2
3. AC: F3-AC3
4. AC: F3-AC4
5. AC: F3-AC1: Klik tautan langkah keputusan membuka formulir yang ada dengan target terisi sesuai IC3 dan tanpa submit otomatis.
6. AC: F3-AC2: Wizard tidak mengirim request penulisan keputusan apa pun.
7. AC: F3-AC3: Bagian OFFICE selalu tampil BLOCKED dengan alasan dan tanpa aksi pada slice ini (office.status konstanta sesuai IC1).
8. AC: F3-AC4: VIEWER tidak melihat tautan keputusan.

## User / Customer Impact

<!-- aeos:section=user_customer_impact anchor=02-requirements--prd-f3--user-customer-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Keputusan lebih tepat sasaran tanpa mengurangi kontrol manusia.

## Dependencies

<!-- aeos:section=dependencies anchor=02-requirements--prd-f3--dependencies content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Fitur `F1`
- F1
- Formulir keputusan yang ada (frontend/src/pages/decisions)
- Kontrak IC3
- Persetujuan manusia atas RFC-2 (checkpoint T12)

## Assumptions

<!-- aeos:section=assumptions anchor=02-requirements--prd-f3--assumptions content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Dukungan prefill via query parameter pada formulir keputusan saat ini diverifikasi T1; perubahan minimal pada formulir mungkin diperlukan (T12).

## Risks

<!-- aeos:section=risks anchor=02-requirements--prd-f3--risks content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Prefill dapat dimanfaatkan untuk mengelabui pemilik agar menyetujui target yang salah (RISK-5)

## Success Metrics

<!-- aeos:section=success_metrics anchor=02-requirements--prd-f3--success-metrics content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Semua langkah DECISION memiliki tautan valid terhadap IC3

## Rollout Considerations

<!-- aeos:section=rollout_considerations anchor=02-requirements--prd-f3--rollout-considerations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Perubahan pada formulir keputusan diisolasi di T12 (R3), dibatasi pada pembacaan parameter allowlist dan memerlukan review keamanan serta persetujuan RFC-2.

## Related Work Items / RFC / TDD

<!-- aeos:section=related_work anchor=02-requirements--prd-f3--related-work content_type=DYNAMIC -->
_Content type: DYNAMIC_

- RFC `03-rfcs/rfc-f3`
- Kontrak api `IC1` — API Penyiapan awal (penyedia `T1`; konsumen `T2`, `T3`, `T4`, `T5`, `T6`, `T7`, `T8`, `T9`, `T11`; `contracts/api/setup-wizard.json`, `contracts/api/routes.json`)
  - Definisi: GET /api/setup/steps -> {catalog_version, host_os, freshness_threshold_seconds, progress, steps[], entry_point{show, blocked_gates[]}, office{status: BLOCKED konstanta, reason}} (VIEWER+). POST /api/setup/steps/{step_key}/recheck -> {step} (peran persis sesuai enumerasi terverifikasi T1; 401/403/404/409/429; CSRF wajib). Field respons allowlist; tidak ada field nilai secret.
  - Kompatibilitas: Versi v1; perubahan hanya aditif; penghapusan field memerlukan versi baru. Jalur positif office di masa depan = penambahan aditif.
- Kontrak schema `IC2` — Skema dan katalog langkah §07 (penyedia `T1`; konsumen `T2`, `T4`, `T6`; `contracts/setup/setup-steps.schema.json`, `contracts/setup/setup-steps.v1.json`)
  - Definisi: Step {step_key, order, title, why, unlocks[], check_ids[], kind, remedy{windows_powershell, macos}, decision_link?}; katalog v1 berisi 10 langkah §07; langkah tanpa check ditandai eksplisit.
  - Kompatibilitas: catalog_version semver; penambahan langkah = minor; perubahan urutan/penghapusan = major.
- Kontrak shared_interface `IC3` — Bentuk prefill formulir keputusan (penyedia `T1`; konsumen `T6`, `T8`, `T12`; `contracts/ui/decision-prefill.json`)
  - Definisi: Per jenis keputusan (worker route activation, process-sandbox exception, write enablement): nama form, daftar query parameter allowlist dan bentuk target; tanpa auto-submit; tanpa nilai secret.
  - Kompatibilitas: Parameter tidak dikenal diabaikan; penambahan parameter aditif.
- Test Plan `06-testing/test-plan-tp1`
- Test Plan `06-testing/test-plan-tp2`
- Rencana: RFC-2
- Rencana: IC3
- Rencana: T6
- Rencana: T12
- Rencana: ADR-0031
- Rencana: ADR-0032
- Rencana: ADR-0051

> Issue/work item per TaskSpec dibuat publisher dan ditautkan sebagai ExternalReference.
>
> Sumber live: GitHub Issues (fadhlillah-dli/dly-aeos). Status dinamis dibaca dari sumber tersebut dan diproyeksikan di AEOS Control Center; dokumen ini tidak menyalin atau menyatakan status.
