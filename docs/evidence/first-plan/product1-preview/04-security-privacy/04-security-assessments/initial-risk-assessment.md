# Initial Risk Assessment

<!-- aeos:logical_id=04-security-assessments/initial-risk-assessment renderer=4 -->

| Metadata | Nilai |
|---|---|
| Logical doc ID | `04-security-assessments/initial-risk-assessment` |
| Project / domain | DLY AEOS (`dly-aeos`) / PERSONAL |
| Owner | UNASSIGNED (Product Owner belum ditetapkan) |
| Status (saat render) | DRAFT |
| Source plan / version | plan `c09825d6-657b-4845-a959-154315737e0f` v2 (`sha256:547a873dfd6c24ca`) — PREVIEW, BELUM DISETUJUI |
| External revision / last synced | dicatat oleh AEOS (DocumentRecord/ExternalReference), tidak ditulis ke isi dokumen |
| Last reviewed | dicatat oleh AEOS per content hash (DocumentRecord), tidak ditulis ke isi dokumen |
| Links | — |
| Issue / evidence | dirujuk live di AEOS (Work items, Evidence) — tidak disalin ke dokumen |

## Context / Scope

<!-- aeos:section=context_scope anchor=04-security-assessments--initial-risk-assessment--context-scope content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Initial risk/security assessment saat project creation (§28), dari rencana yang disetujui. Ini identifikasi awal, bukan hasil pengujian security.

Clone baru sudah bisa mencapai dashboard, tetapi pemilik masih harus membaca tabel Readiness Doctor dan README untuk menentukan urutan langkah manual §07. Slice ini menambahkan halaman 'Penyiapan awal' yang menyusun Doctor check yang ada menjadi langkah berurutan. Halaman menampilkan status beserta kesegarannya, perintah yang bisa disalin per OS, tombol 'Periksa ulang' per langkah, dan tautan ke formulir keputusan yang sudah ada.

**Pertanyaan terbuka security/privacy (rencana)**

- Apakah detail/remedy Doctor saat ini pernah memuat jalur yang berisi nama pengguna OS? (diperiksa T1; menentukan personal_data F1)
- Peran mana persisnya yang boleh memicu recheck? (enumerasi diverifikasi T1; dijawab manusia di checkpoint T1)
- Ambang kesegaran (staleness) Doctor yang berlaku (diverifikasi T1; fallback nilai ADR perlu persetujuan manusia).
- Apakah perubahan formulir keputusan untuk prefill memerlukan ADR baru terkait ADR-0031? (bagian dari persetujuan RFC-2)

## Risk Register

<!-- aeos:section=risk_register anchor=04-security-assessments--initial-risk-assessment--risk-register content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

| ID | Risiko | Likelihood | Impact | Owner |
|---|---|---|---|---|
| RISK-1 | Kebocoran secret atau data sensitif lewat remedy/detail Doctor — Detail atau remedy Doctor dapat memuat jalur lokal, nama pengguna OS atau fragmen kredensial yang kemudian dirender halaman. | MEDIUM | HIGH | — |
| RISK-2 | Penyalahgunaan recheck — Recheck berulang dapat memicu panggilan konektivitas eksternal atau operasi mahal dari check. | LOW | MEDIUM | — |
| RISK-3 | Pemetaan langkah ke check salah atau tidak lengkap — Langkah §07 dapat terpetakan ke check yang salah atau tidak memiliki check, sehingga status menyesatkan atau nilai wizard turun bila banyak langkah UNKNOWN. | MEDIUM | MEDIUM | — |
| RISK-4 | Eskalasi hak akses pada endpoint recheck — Kesalahan gate peran dapat membuat VIEWER memicu eksekusi check. | LOW | HIGH | — |
| RISK-5 | Manipulasi prefill formulir keputusan — Tautan prefill yang dibuat pihak lain dapat mengarahkan pemilik menyetujui target yang salah (mis. rute atau write enablement). | LOW | HIGH | — |
| RISK-6 | Status menyesatkan karena kesegaran — Hasil lama ditampilkan seolah terkini sehingga pemilik mengira gate terbuka. | MEDIUM | MEDIUM | — |
| RISK-7 | Kelayakan F2 bergantung pada dukungan subset runner Doctor — F2 (P0) memerlukan eksekusi subset check per check_id. Bila runner tidak mendukungnya, T4 berhenti dan M2 tertunda. | UNKNOWN | HIGH | — |

## Likelihood / Impact

<!-- aeos:section=likelihood_impact anchor=04-security-assessments--initial-risk-assessment--likelihood-impact content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

Rating mengikuti nilai di register; ambang risiko kanonik (MCI) UNVERIFIED — tidak dikarang.

## Mitigations

<!-- aeos:section=mitigations anchor=04-security-assessments--initial-risk-assessment--mitigations content_type=AI_MAINTAINED -->
_Content type: AI_MAINTAINED_

- RISK-1: T1 memeriksa contoh remedy/detail Doctor dan merekomendasikan klasifikasi personal_data F1. Remedy diambil dari template katalog/remedy Doctor yang sudah disamarkan. Mitigasi lain: respons allowlist, tes pola secret dan pola jalur home (C:\Users\*, /Users/*) dengan nilai sintetis di T2/T8, dan review keamanan T8.
- RISK-2: Lock per langkah (409), batas laju (429), timeout yang dapat dikonfigurasi, hanya peran berwenang.
- RISK-3: Tabel pemetaan T1 dengan lokasi kode menjadi checkpoint manusia. Usulan: bila lebih dari 3 langkah tanpa check, manusia memutuskan apakah slice dilanjutkan. Katalog divalidasi terhadap registri check (tes: setiap check_id ada), dan langkah tanpa check tampil UNKNOWN.
- RISK-4: Memakai dependensi otorisasi yang ada dengan enumerasi peran terverifikasi T1, tes negatif 401/403/CSRF, review keamanan, dan tambahan pada tests/acceptance/test_auth_boundary.py bila sesuai.
- RISK-5: Perubahan diisolasi di T12 (R3) dan baru dimulai setelah RFC-2 disetujui manusia. Mitigasi lain: allowlist field, tanpa auto-submit, semua field ditampilkan untuk ditinjau, binding keputusan ADR-0031 tetap berlaku, dan review keamanan.
- RISK-6: Freshness eksplisit per check dengan ambang terdokumentasi (aturan Doctor atau ADR), freshness_threshold_seconds di respons, STALE tidak dihitung dalam progres, checked_at selalu ditampilkan.
- RISK-7: T1 memverifikasi dukungan subset. Pertanyaan material dibuat blocking untuk M2, dengan opsi fallback untuk keputusan manusia: (a) task terpisah R3 untuk perluasan runner di path terlindungi, (b) F2 diturunkan menjadi 'jalankan Doctor penuh lalu tampilkan hasil langkah', atau (c) F2 ditunda.

## Residual Risk

<!-- aeos:section=residual_risk anchor=04-security-assessments--initial-risk-assessment--residual-risk content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

Usulan residual risk dari rencana (DATA — bukan risk acceptance):

- RISK-1: Belum dinilai; penerimaan risiko residual hanya melalui keputusan RISK_ACCEPTANCE manusia.
- RISK-2: Belum dinilai; keputusan manusia.
- RISK-3: Belum dinilai; keputusan manusia.
- RISK-4: Belum dinilai; keputusan manusia.
- RISK-5: Belum dinilai; keputusan manusia.
- RISK-6: Belum dinilai; keputusan manusia.
- RISK-7: Belum dinilai; keputusan manusia.

## Risk Owner

<!-- aeos:section=risk_owner anchor=04-security-assessments--initial-risk-assessment--risk-owner content_type=STATIC -->
_Content type: STATIC_

UNASSIGNED (Product Owner belum ditetapkan)

## Risk Acceptance

<!-- aeos:section=acceptance anchor=04-security-assessments--initial-risk-assessment--acceptance content_type=HUMAN_GOVERNED -->
_Content type: HUMAN_GOVERNED_

_HUMAN_GOVERNED — diisi oleh manusia yang berwenang (keputusan / risk acceptance / approval). AEOS dan model tidak mengisi bagian ini._

Risk acceptance hanya melalui Decision manusia (kind RISK_ACCEPTANCE).
