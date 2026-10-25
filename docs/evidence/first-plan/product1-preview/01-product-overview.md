# 01. Product Overview

<!-- aeos:logical_id=01-product-overview renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `01-product-overview` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Product Owner belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Overview

<!-- aeos:section=overview anchor=01-product-overview--overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Clone baru sudah bisa mencapai dashboard, tetapi pemilik masih harus membaca tabel Readiness Doctor dan README untuk menentukan urutan langkah manual §07. Slice ini menambahkan halaman 'Penyiapan awal' yang menyusun Doctor check yang ada menjadi langkah berurutan. Halaman menampilkan status beserta kesegarannya, perintah yang bisa disalin per OS, tombol 'Periksa ulang' per langkah, dan tautan ke formulir keputusan yang sudah ada.

## Vision / Objectives

<!-- aeos:section=vision_objectives anchor=01-product-overview--vision-objectives content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**Vision:** Pemilik dapat menyelesaikan bootstrap §07 dari clone kosong sampai gate CAN_* PERSONAL terbuka tanpa menebak urutan. Status selalu bersumber dari Doctor, dan setiap keputusan tetap dicatat manusia lewat antrean keputusan yang ada.

- Satu langkah berurutan per langkah bootstrap §07. Setiap langkah menampilkan capability yang dibuka, status Doctor + waktu pemeriksaan + kesegaran, dan perintah untuk Windows PowerShell / macOS.
- Progres dihitung hanya dari hasil Doctor; STALE/UNKNOWN ditampilkan apa adanya.
- Periksa ulang per langkah yang hanya menjalankan check milik langkah tersebut, terbatas pada peran yang berwenang.
- Langkah keputusan manusia hanya menautkan ke formulir keputusan yang ada dengan target sudah terisi; wizard tidak pernah mencatat keputusan atau menjalankan perintah.
- Titik masuk dari halaman Ringkasan selama ada gate CAN_* PERSONAL yang BLOCKED.
- Tidak ada nilai secret, token atau kata sandi yang dirender atau dikirim halaman.

## Scope

<!-- aeos:section=scope anchor=01-product-overview--scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

**In scope**

- Discovery terverifikasi atas asumsi kritis (T1) sebelum implementasi
- Katalog langkah §07 statis (berversi) yang memetakan langkah ke Doctor check, capability gate dan remedy yang sudah ada
- API baca status penyiapan dan API periksa ulang per langkah
- Halaman frontend 'Penyiapan awal' (read-only untuk VIEWER) beserta registrasi rute/menu
- Deep-link ke formulir keputusan (aktivasi rute worker, pengecualian process-sandbox, pengaktifan write) dengan target terisi
- Banner/titik masuk di halaman Ringkasan
- Penanda OFFICE tetap BLOCKED (konstanta pada slice ini)
- Tes acceptance, negatif dan keamanan; pembaruan dokumentasi setup

**Out of scope / non-goals**

- Auto-install atau eksekusi perintah apa pun oleh wizard
- Input secret/token/kata sandi di browser
- Langkah setup OFFICE selain menampilkan status BLOCKED
- Doctor check baru atau perubahan semantik capability gate
- Perubahan runner Doctor (kecuali melalui keputusan manusia atas pertanyaan material subset run)
- Pencatatan keputusan oleh wizard
- State progres milik wizard sendiri (persisten atau lokal)
- Implementasi metrik/alert baru (hanya usulan operasional)

**Asumsi rencana**

- source: GitHub repository (UNVERIFIED until the integration check passes) (source: §04 domain profile)
- docs: Google Docs in the project's Google Drive folder (UNVERIFIED until the integration check passes) (source: §04 domain profile)
- work_tracking: GitHub Issues (UNVERIFIED until the integration check passes) (source: §04 domain profile)
- ci: GitHub Actions (UNVERIFIED until the integration check passes) (source: §04 domain profile)
- pr: GitHub pull request (UNVERIFIED until the integration check passes) (source: §04 domain profile)
- Doctor menyimpan hasil terakhir per check beserta waktu pemeriksaan yang dapat dibaca tanpa menjalankan ulang check. (source: Permintaan (status dengan freshness) dan ADR-0011; diverifikasi oleh laporan discovery T1)
- Perintah remedy ditampilkan untuk OS host backend dengan tab untuk OS lain. (source: Usulan perencana)
- Lokasi pendaftaran router backend dan rute/menu frontend dilaporkan T1. allowed_paths T11 diperbarui melalui versi rencana baru yang disetujui manusia. (source: Inventaris repo (tidak memuat detail modul); temuan challenger MD1)

**Unknowns rencana (belum diputuskan)**

- MCI_V2 canonical policy is UNVERIFIED; no compliance claim can be made (impact: Item platform-level. Tidak ada fitur atau gate dalam slice ini yang bergantung padanya; slice ini tidak membuat klaim kepatuhan MCI.)
- Apakah runner Doctor mendukung eksekusi subset check per check_id. (impact: Diverifikasi T1. Jika tidak, T4 tidak dimulai sampai manusia memilih fallback (pertanyaan material blocking untuk M2).)
- Ambang kesegaran (staleness) Doctor yang berlaku. (impact: Diverifikasi T1. Bila tidak ada, ambang eksplisit ditetapkan di ADR dan disetujui manusia sebelum T2.)
- Apakah setiap dari 10 langkah §07 memiliki Doctor check yang ada (hanya adapter.github_token dan adapter.google_oauth yang terlihat di input). (impact: Tabel pemetaan T1 menjadi checkpoint. Langkah tanpa check tampil UNKNOWN; bila jumlahnya banyak, manusia memutuskan kelanjutan slice.)
- Apakah formulir keputusan saat ini mendukung prefill. (impact: Diverifikasi T1; menentukan besarnya perubahan T12.)
- Lokasi file pendaftaran router backend dan rute/menu frontend. (impact: Dilaporkan T1; allowed_paths T11 direvisi sebelum T11 dimulai.)
- Daftar path terlindungi proyek self-build (§09) dan apakah routes.json, module-boundaries.json serta pages/decisions termasuk di dalamnya. (impact: Diverifikasi T1. Path terlindungi hanya diubah setelah keputusan manusia.)
- Model proses server Control Center (jumlah worker proses). (impact: Menentukan validitas lock in-memory recheck; dikonfirmasi T1.)
- Working tree inventaris dalam keadaan dirty (1 entri) dan kredensial integrasi UNKNOWN (secret store unavailable). (impact: Baseline bisa berbeda dari commit; verifier tepercaya menjalankan suite baseline sebelum T2. Publikasi GitHub Issues/Drive belum terverifikasi.)

## Team / Ownership

<!-- aeos:section=team_ownership anchor=01-product-overview--team-ownership content_type=STATIC -->
_Content type: STATIC_

UNKNOWN (tidak ada pemilik tim yang disebutkan dalam input).

| Peran | Nama |
|---|---|
| Security Lead | UNKNOWN (pemilik keamanan tidak disebutkan; keputusan risiko tetap pada pemilik manusia). |

## Stakeholders

<!-- aeos:section=stakeholders anchor=01-product-overview--stakeholders content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- Pemilik/owner DLY AEOS (manusia tunggal yang menyetujui rencana, merge dan rilis; nama tidak disebutkan)
- Pengguna peran VIEWER Control Center (disebut dalam permintaan)

## Roadmap

<!-- aeos:section=roadmap anchor=01-product-overview--roadmap content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- M1 — Discovery, kontrak & halaman baca: laporan discovery, katalog langkah §07, API status, halaman Penyiapan awal read-only dan registrasi rute/menu (F1)
- M2 — Aksi terkendali: Periksa ulang per langkah (F2), prefill formulir keputusan dan tautan keputusan + penanda OFFICE (F3)
- M3 — Titik masuk & pengerasan: banner Ringkasan (F4), tes keamanan/acceptance E2E, dokumentasi, rencana rilis

| Milestone | Fitur | Exit evidence |
|---|---|---|
| Discovery, kontrak dan halaman baca Penyiapan awal | Halaman Penyiapan awal (read-only) berbasis Doctor | Bukti berikut harus tersedia: (1) bukti run baseline oleh verifier tepercaya; (2) laporan discovery T1 dan kontrak IC1-IC3 diterima manusia; (3) revisi allowed_paths T11 disetujui; (4) tes unit/kontrak/komponen F1 (T2, T3, T11) lulus di verifier tepercaya; (5) PR di-review. Bukti acceptance E2E F1 dipenuhi di M3 (T9). |
| Periksa ulang dan tautan keputusan | Periksa ulang per langkah, Langkah keputusan manusia dan penanda OFFICE | Pertanyaan subset run dijawab manusia dan RFC-2 disetujui. Tes F2 dan F3 (termasuk negatif 401/403/404/409/429/CSRF) lulus di verifier tepercaya. |
| Titik masuk Ringkasan, pengerasan dan dokumentasi | Titik masuk dari Ringkasan | Tes F4, TP2 dan acceptance E2E T9 lulus di verifier tepercaya; laporan review keamanan dan dokumentasi tersedia; rencana rilis siap untuk keputusan manusia. |

**Governance milestone (rencana)**

- **Discovery, kontrak dan halaman baca Penyiapan awal** — prioritas P0
  - Acceptance: Bukti run baseline oleh verifier tepercaya tercatat
  - Acceptance: Laporan discovery T1 dan IC1-IC3 diterima manusia
  - Acceptance: Revisi allowed_paths T11 disetujui manusia
  - Acceptance: F1-AC1, F1-AC3..AC6 dibuktikan tes unit/kontrak/komponen (T2, T3) dan registrasi rute (T11) lulus di verifier tepercaya
  - Acceptance: Acceptance E2E F1 (F1-AC1, F1-AC2) ditunda ke M3 melalui T9
- **Periksa ulang dan tautan keputusan** — prioritas P0; bergantung pada `M1`
  - Acceptance: Pertanyaan material subset run dijawab manusia (blocking)
  - Acceptance: RFC-2 disetujui manusia sebelum T12
  - Acceptance: F2-AC1..AC6 dan F3-AC1..AC4 lulus di verifier tepercaya
- **Titik masuk Ringkasan, pengerasan dan dokumentasi** — prioritas P1; bergantung pada `M2`
  - Acceptance: F4-AC1..AC3, F1-AC7, TP2 dan acceptance E2E T9 lulus di verifier tepercaya
  - Acceptance: Laporan review keamanan dan dokumentasi tersedia

## System / Service Inventory

<!-- aeos:section=system_inventory anchor=01-product-overview--system-inventory content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- backend/aeos (Python, 579 file Python di repo) — Readiness Doctor, capability gates, decision endpoints (lokasi modul persis UNKNOWN; dikonfirmasi oleh laporan discovery T1)
- frontend/src/pages/readiness (Doctor UI, RemedyPanel)
- frontend/src/pages/overview (Ringkasan)
- frontend/src/pages/decisions (formulir keputusan)
- contracts/api/routes.json (kontrak rute API)
- contracts/architecture/module-boundaries.json
- docs/SETUP_WINDOWS.md, docs/SETUP_MACOS.md
- tests/acceptance/test_bootstrap_readiness.py, tests/acceptance/test_auth_boundary.py
- DLY AEOS Control Center (backend Python + frontend TypeScript)

## Data Overview

<!-- aeos:section=data_overview anchor=01-product-overview--data-overview content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Fitur hanya membaca hasil Doctor (status, check_id, checked_at, remedy) dan status capability gate; data berklasifikasi INTERNAL. Tidak ada tabel baru (katalog langkah berupa file berversi di repo). Tidak ada nilai secret yang dibaca atau ditampilkan; remedy yang mengandung penanda [REDACTED] tetap disamarkan. Apakah remedy/detail memuat jalur home (nama pengguna OS) diverifikasi T1.

- Hasil Doctor (status, check_id, checked_at, remedy tersamarkan) (INTERNAL)
- Katalog langkah §07 (INTERNAL)
- Event audit recheck (INTERNAL)
