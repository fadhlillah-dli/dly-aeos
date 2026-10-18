# §50 acceptance matrix → tests (trusted suite coverage tracker)

Every row of master §50 (all three tables) is listed exactly once. Status:

* **ACCEPTANCE** — covered by the trusted acceptance suite in this directory (runs against candidate
  code through the independent verifier; protected path).
* **UNIT** — covered by module tests (`tests/unit`, `tests/integration`) but not yet promoted into the
  trusted suite. Promotion = copy the scenario here once the owning module is merged.
* **NOT_IMPLEMENTED** — no test yet; the owning module must add one (unit first, then promote here).
* **BLOCKED** — cannot be proven on this machine/repo without an external capability (named).

Test ids use `file::test` relative to `tests/`. Owning modules follow master §10 / `backend/aeos/<module>`.

## Table 1 — Acceptance matrix untuk implementasi

| # | §50 row | Status | Test id(s) / remedy | Owner |
|---|---|---|---|---|
| 1.1 | Dependency inti belum terpasang | NOT_IMPLEMENTED | bootstrap CLI reason/remedy (`aeos doctor`) | doctor |
| 1.2 | Tool opsional atau Office belum ready | NOT_IMPLEMENTED | dashboard stays usable, capability-scoped readiness | doctor, control_center |
| 1.3 | Port lokal konflik | ACCEPTANCE | `acceptance/test_runtime_binding.py::test_port_conflict_is_explained_not_rebound`, `::test_only_loopback_binding` | core (cli) |
| 1.4 | Doctor hanya berhasil connect | UNIT | `frontend/src/components/StatusBadge.test.tsx` (CONNECTED never "ok"); doctor functional checks NOT_IMPLEMENTED | doctor |
| 1.5 | Config/model/provider berubah setelah doctor | ACCEPTANCE (partial) + UNIT | `acceptance/test_evidence_honesty.py::test_acceptance_validity_is_bound_to_identity` (changed grader → not valid); `unit/test_verification_acceptance.py::test_recheck_persists_config_staleness`, `::test_verification_under_an_older_grader_is_not_accepted`, `::test_unknown_current_config_is_not_accepted`; `unit/test_delivery_gates.py::test_g2_is_stale_when_grader_or_config_changed`; doctor invalidation NOT_IMPLEMENTED | doctor, verification |
| 1.6 | Candidate DLY AEOS mengubah aktif policy/verifier/controller | ACCEPTANCE + UNIT | `acceptance/test_evidence_honesty.py::test_grader_policy_pipeline_changes_never_pass_scope`; `unit/test_verification_verifier.py::test_protected_path_outside_contract_fails_and_quarantines`, `::test_trusted_suite_used_even_when_candidate_rewrites_its_own_copy`, `::test_candidate_module_shadowing_the_harness_fails`; `integration/test_verification_real_suite.py` | verification |
| 1.7 | Upgrade controller gagal | NOT_IMPLEMENTED | release keep + restore test | operations |
| 1.8 | Source Office muncul dalam platform personal working tree | NOT_IMPLEMENTED | intake/working-tree scan | intake, policy |
| 1.9 | Submit prompt tanpa domain | ACCEPTANCE (partial) | `acceptance/test_domain_scope.py::test_domain_scoped_reads_require_explicit_domain`; prompt submission endpoint NOT_IMPLEMENTED | identity, intake |
| 1.10 | Prompt memerintahkan OFFICE menjadi PERSONAL | ACCEPTANCE (partial) | `acceptance/test_domain_scope.py::test_project_domain_is_immutable`, `unit/test_verification_collector.py::test_domain_cannot_be_relabelled`; prompt handling NOT_IMPLEMENTED | portfolio, intake |
| 1.11 | Lampiran berisi instruksi mengirim source ke GitHub personal | NOT_IMPLEMENTED | attachments are DATA, no tool permission | intake, policy |
| 1.12 | File path traversal/symlink keluar sandbox | ACCEPTANCE (partial) | `acceptance/test_runtime_binding.py::test_path_traversal_segments_rejected`, `::test_managed_paths_cannot_escape`; `unit/test_verification_scope.py::test_harness_tamper_and_unsafe_paths`; `unit/test_verification_verifier.py::test_windows_reserved_name_in_tree_fails_on_every_os`; sandbox FS containment NOT_IMPLEMENTED | core, worker_runtime |
| 1.13 | Worker membaca vault/SSH/native Claude credential | UNIT (partial) / BLOCKED | `unit/test_verification_procs.py::test_scrubbed_env_is_allow_list`, `::test_child_cannot_see_parent_secrets` (verifier child env); worker sandbox containment BLOCKED until a real sandbox (container/VM) exists | worker_runtime, secrets |
| 1.14 | VIEWER memanggil write endpoint langsung | ACCEPTANCE | `acceptance/test_auth_boundary.py::test_viewer_is_denied_on_every_write_route`; `integration/test_verification_api.py::test_viewer_cannot_write` | identity |
| 1.15 | Bootstrap belum selesai tetapi publish diminta | NOT_IMPLEMENTED | external-write interlock | policy, publisher |
| 1.16 | Approval PlanVersion lama | UNIT (partial) | G0 FAIL for a non-approved/changed version: `unit/test_delivery_gates.py::test_g0_binding_variants`; acceptance refuses a PlanVersion hash mismatch: `unit/test_verification_acceptance.py::test_plan_version_hash_mismatch_is_not_accepted`; approval binding/UI NOT_IMPLEMENTED | planning, policy |
| 1.17 | Revisi baru muncul selama publish | NOT_IMPLEMENTED | job uses approved version | publisher |
| 1.18 | Google Docs direvisi manusia | NOT_IMPLEMENTED | requiredRevisionId conflict | publisher, docs_engine |
| 1.19 | Tenant tidak mendukung folder Confluence yang wajib | NOT_IMPLEMENTED | block/needs action (OFFICE) | publisher |
| 1.20 | Provider timeout setelah create resource | NOT_IMPLEMENTED | reconcile before retry | publisher |
| 1.21 | Salah satu target publikasi gagal | NOT_IMPLEMENTED | PARTIAL, no re-create | publisher |
| 1.22 | Jira/GitHub issue Done tanpa evidence runtime | UNIT (partial) | G2 only from verifier: `unit/test_delivery_gates.py::test_fresh_task_nothing_is_pass`; issue projection NOT_IMPLEMENTED | workforce, publisher |
| 1.23 | Worker exit 0 / menulis "all tests pass" | ACCEPTANCE + UNIT | `acceptance/test_evidence_honesty.py::test_worker_evidence_is_an_untrusted_claim`, `::test_worker_exit_zero_yields_no_acceptance_and_no_pass`; `unit/test_verification_verifier.py::test_worker_claimed_pass_never_counts` | verification |
| 1.24 | Worker mengubah grader/policy/pipeline di luar kontrak | ACCEPTANCE + UNIT | `acceptance/test_evidence_honesty.py::test_grader_policy_pipeline_changes_never_pass_scope`; `unit/test_verification_verifier.py::test_protected_path_outside_contract_fails_and_quarantines`, `::test_harness_tamper_file_fails`, `::test_candidate_module_shadowing_the_harness_fails` | verification |
| 1.25 | PR baru mengubah head atau base terkait | ACCEPTANCE (partial) + UNIT | `acceptance/test_evidence_honesty.py::test_acceptance_validity_is_bound_to_identity`; `unit/test_verification_acceptance.py::test_stale_on_head_change_and_identity_checks`; `unit/test_delivery_pr_bundle.py::test_sync_new_commits_make_acceptance_stale`, `::test_sync_base_moved_makes_acceptance_stale`, `::test_branch_update_hook_invalidates`, `::test_task_branch_moved_after_acceptance_is_refused_before_push`; `unit/test_verification_verifier.py::test_head_not_descending_from_base_is_not_proven`; re-verification after STALE needs a workforce facade (shared change request) | verification, delivery |
| 1.26 | Worker A dan B menulis shared schema yang sama | NOT_IMPLEMENTED | admission serialises overlapping write sets | workforce |
| 1.27 | Controller crash dan worker lama masih aktif | NOT_IMPLEMENTED | lease reconciliation | workforce |
| 1.28 | Cancel saat ada child process | UNIT (partial) | verifier kills the process tree on timeout: `unit/test_verification_procs.py::test_timeout_kills_process_tree`; run cancel NOT_IMPLEMENTED | workforce, worker_runtime |
| 1.29 | Free quota habis / 429 | NOT_IMPLEMENTED | backoff/queue, no paid fallback | routing, cost |
| 1.30 | Fable Max quota habis | UNIT | `unit/test_verification_review.py::test_quota_exhausted_waits_and_opens_nothing`, `unit/test_delivery_gates.py::test_quota_waiting_review_blocks_g4`; planning-side waiting NOT_IMPLEMENTED | verification, claude_bridge, planning |
| 1.31 | Model alias/provider route berubah | NOT_IMPLEMENTED | new config/UNKNOWN, no baseline reuse; reviewer model policy: `unit/test_verification_review.py::test_model_policy_mismatch_is_not_proven` | routing, evaluation |
| 1.32 | Budget DB/gateway tidak sehat | NOT_IMPLEMENTED | admission refused | cost |
| 1.33 | Agent mencoba direct push main/merge API | UNIT (partial) / BLOCKED | no merge/deploy code: `unit/test_delivery_no_merge.py::*`; non-human merge → G7 FAIL: `unit/test_delivery_gates.py::test_non_human_merge_fails_g7_and_head_mismatch_fails_g5`; broker refspec/merge-API boundary tests NOT_IMPLEMENTED (broker); provider enforcement BLOCKED on GitHub Free private repos (`NOT_ENFORCEABLE`, `unit/test_delivery_provider.py::test_free_private_403_upgrade_is_not_enforceable`) | broker, delivery |
| 1.34 | PR code mencoba membaca production secret | UNIT (partial) | verifier child env has no secrets: `unit/test_verification_procs.py::test_child_cannot_see_parent_secrets`, `::test_git_env_is_an_allow_list_without_credentials`; CI PR builds get no secrets and a read-only token, actions pinned: `unit/test_delivery_ci_secrets.py::*`; worker sandbox NOT_IMPLEMENTED | worker_runtime, secrets |
| 1.35 | Deploy sukses tetapi fungsi utama gagal | UNIT (partial) / BLOCKED | PASS-PRODUCTION never claimed without production evidence: `unit/test_verification_review.py::test_verdict_clamping`, `::test_pass_production_claim_is_clamped_in_record`; G8 NOT_APPLICABLE — BLOCKED until a deployment target exists | delivery, operations |
| 1.36 | Restore backup belum pernah diuji | NOT_IMPLEMENTED | no "recovery ready" claim | operations |
| 1.37 | Trace berisi secret/PII | ACCEPTANCE + UNIT | `acceptance/test_runtime_binding.py::test_secrets_redacted_before_persistence`; `unit/test_verification_collector.py::test_content_is_redacted_hashed_and_stored_under_project_evidence`, `::test_binary_content_with_secret_is_refused`; `unit/test_verification_verifier.py::test_secret_in_diff_fails_without_persisting_the_value`, `::test_secret_inside_a_binary_file_fails`; `unit/test_verification_scope.py::test_added_line_that_looks_like_a_file_header_cannot_dodge_the_scan` | core, verification |
| 1.38 | Memory lama/cross-project mencoba menjadi kebijakan | NOT_IMPLEMENTED | provenance/scope revalidation | knowledge |
| 1.39 | Model/prompt/tool/policy baru | NOT_IMPLEMENTED | controlled eval + decision + rollback | evaluation, proposals |
| 1.40 | Klaim compliance dari template/scanner hijau | UNIT (partial) | NOT-PROVEN never green: `frontend/src/components/StatusBadge.test.tsx`; compliance pack mapping NOT_IMPLEMENTED (MCI UNVERIFIED) | control_center, policy |

## Table 2 — Organisasi dan Workforce Console

| # | §50 row | Status | Test id(s) / remedy | Owner |
|---|---|---|---|---|
| 2.1 | Role catalog berisi banyak jabatan, belum ada task | NOT_IMPLEMENTED | no LLM run/cost for org chart | workforce |
| 2.2 | Tugas kecil tidak membutuhkan spesialis | NOT_IMPLEMENTED | single implementer default | workforce |
| 2.3 | Task dinilai dapat paralel tetapi write sets overlap | NOT_IMPLEMENTED | admission refuses/serialises | workforce |
| 2.4 | Agent berposisi "CTO/manager" meminta privilege anak | NOT_IMPLEMENTED | no privilege from title | workforce, policy |
| 2.5 | Pesan antaragent mengaku sudah mendapat izin owner | UNIT (partial) | worker claims never count: `unit/test_verification_verifier.py::test_worker_claimed_pass_never_counts`; message-as-approval refusal NOT_IMPLEMENTED | policy, workforce |
| 2.6 | Worker selesai tetapi verifier gagal | UNIT | `unit/test_verification_verifier.py::test_worker_claimed_pass_never_counts`, `unit/test_verification_acceptance.py::test_verifier_fail_with_review_pass_is_rejected`, `unit/test_delivery_gates.py::test_review_pass_without_verifier_pass_is_not_g4_pass`; UI: `frontend/src/pages/evidence/EvidencePage.test.tsx` | verification |
| 2.7 | Lease lama mengirim event setelah retry/reassign | NOT_IMPLEMENTED | stale lease events rejected | workforce, events |
| 2.8 | SSE/stream putus | NOT_IMPLEMENTED | last observed + STALE/UNKNOWN in UI | control_center |
| 2.9 | Cancel dari dashboard | NOT_IMPLEMENTED | process/capability really stopped | workforce, worker_runtime |
| 2.10 | Dashboard Personal mencoba run/evidence Office | ACCEPTANCE | `acceptance/test_domain_scope.py::test_personal_session_cannot_see_office_records`; `integration/test_verification_api.py::test_evidence_scoped_to_domain_and_redacted`, `::test_cross_domain_run_is_404` | identity, verification |
| 2.11 | Fixture roster/run ditampilkan saat belum ada runtime | UNIT (partial) | MOCK evidence/review never accepted: `unit/test_verification_acceptance.py::test_mock_verification_never_accepts`, `acceptance/test_evidence_honesty.py::test_mock_acceptance_is_never_valid`; roster labelling NOT_IMPLEMENTED | workforce, control_center |
| 2.12 | Role/model/mode diganti | NOT_IMPLEMENTED | versioned config, history unchanged | workforce, evaluation |
| 2.13 | Candidate skill meminta auto-update policy | UNIT (partial) | policy paths quarantined: `unit/test_verification_scope.py::test_protected_path_outside_contract_fails_case_insensitively`; versioned promotion NOT_IMPLEMENTED | policy, proposals |
| 2.14 | Controller membangun versi AEOS baru | UNIT + INTEGRATION | `unit/test_verification_verifier.py::test_trusted_suite_used_even_when_candidate_rewrites_its_own_copy`; `integration/test_verification_real_suite.py::test_real_suite_passes_for_clean_candidate_and_catches_regression` | verification |

## Table 3 — Transparansi tools/repo dan perubahan arsitektur

| # | §50 row | Status | Test id(s) / remedy | Owner |
|---|---|---|---|---|
| 3.1 | Katalog blueprint berisi banyak tool tetapi host belum diprobe | NOT_IMPLEMENTED | PLANNED/CANDIDATE/UNKNOWN only | inventory |
| 3.2 | Tool ditemukan dalam README atau lockfile | NOT_IMPLEMENTED | reference ≠ active | inventory |
| 3.3 | Tool installed dan enabled tanpa run yang teramati | NOT_IMPLEMENTED | not IN_USE | inventory |
| 3.4 | Model request sudah selesai | NOT_IMPLEMENTED | LAST_USED with run/time | inventory, routing |
| 3.5 | Probe connectivity berhasil tetapi functional check gagal | NOT_IMPLEMENTED | CONNECTED + FAILED | doctor, inventory |
| 3.6 | Binary/model route/config berganti dari approved pin | UNIT (partial) | verifier config hash invalidates acceptance and G2: `unit/test_verification_acceptance.py::test_recheck_persists_config_staleness`, `unit/test_delivery_gates.py::test_g2_is_stale_when_grader_or_config_changed`; drift detection NOT_IMPLEMENTED | inventory, doctor |
| 3.7 | Upstream release baru terdeteksi | NOT_IMPLEMENTED | proposal only, no auto-install | research, proposals |
| 3.8 | Source repo rename/redirect/fork/publisher berubah | NOT_IMPLEMENTED | identity re-check | inventory, research |
| 3.9 | Nama repo ambigu atau sumber tidak terverifikasi | UNIT (partial) | provider checks only registered repos: `integration/test_verification_api.py::test_provider_capabilities_requires_registered_repo`; inventory UNVERIFIED admission NOT_IMPLEMENTED | inventory |
| 3.10 | Komponen ditemukan di managed scope tanpa registrasi | NOT_IMPLEMENTED | UNREGISTERED observation | inventory |
| 3.11 | Worker memanggil binary/tool/endpoint tidak admitted | NOT_IMPLEMENTED | broker/boundary refuses | broker, worker_runtime |
| 3.12 | Inventory/stream gagal atau offline | NOT_IMPLEMENTED | last snapshot + STALE | inventory, control_center |
| 3.13 | Viewer mengakses detail/search/export inventory Office | ACCEPTANCE (partial) | evidence/run scope: `acceptance/test_domain_scope.py::test_personal_session_cannot_see_office_records`; inventory endpoints NOT_IMPLEMENTED | inventory, identity |
| 3.14 | Source/tool output memasukkan secret/instruksi berbahaya | ACCEPTANCE (partial) | redaction: `acceptance/test_runtime_binding.py::test_secrets_redacted_before_persistence`; reviewer treats worker claims as DATA (`verification/review.py` prompt) — not proven against a live model | core, verification |
| 3.15 | Pengguna hanya meminta compare/review URL | NOT_IMPLEMENTED | no install/activate/send | research, proposals |
| 3.16 | Kandidat lebih cepat/murah tetapi gagal authority/security gate | NOT_IMPLEMENTED | hard gates beat aggregate score | evaluation, proposals |
| 3.17 | Trial disetujui, promotion belum disetujui | NOT_IMPLEMENTED | baseline stays active | proposals, policy |
| 3.18 | Baseline/candidate/target berubah setelah approval | UNIT (partial) | acceptance bound to artifact/base/head/config/plan hashes: `unit/test_verification_acceptance.py::test_stale_on_head_change_and_identity_checks`, governance rejects changed hash: `unit/test_delivery_governance.py::test_changed_artifact_hash_is_rejected_before_adapter`; approval re-binding NOT_IMPLEMENTED | policy, proposals |
| 3.19 | Replace mengubah caller/schema/data/CI | UNIT (partial) | review non-regression scope hints (callers/contracts/state): `unit/test_verification_review.py::test_pass_review_records_review_verify_verdict_and_accepts`; migration/consumer tests NOT_IMPLEMENTED | proposals |
| 3.20 | Dua orchestration tools hendak menjadi scheduler aktif | NOT_IMPLEMENTED | single scheduler authority | workforce, proposals |
| 3.21 | Migration gagal atau rollback data tidak mungkin | UNIT (partial) | PR bundle states code revert ≠ data rollback: `unit/test_delivery_pr_bundle.py::test_bundle_contains_hashes_unknowns_rollback_and_human_only_statement`; migration stop/partial NOT_IMPLEMENTED | operations, delivery |
| 3.22 | Canary bukan metode aman bagi komponen | NOT_IMPLEMENTED | sandbox/staging/cutover | proposals, operations |
| 3.23 | Runtime aktif tetapi external docs publication conflict | NOT_IMPLEMENTED | ACTIVE vs DOCS_PUBLISHED separated | publisher, docs_engine |
| 3.24 | GDocs/Confluence diedit manusia selama update | NOT_IMPLEMENTED | revision conflict respected | publisher |
| 3.25 | Snapshot export dibuka setelah runtime berubah | NOT_IMPLEMENTED | scope/time/hash shown | control_center |
| 3.26 | Nama master tetap Final setelah perubahan sah | NOT_IMPLEMENTED | same file, history kept | docs_engine |
| 3.27 | Mock diagram, roster, costs atau tests ditampilkan | UNIT (partial) | MOCK never counts as accepted/E3: `unit/test_verification_acceptance.py::test_mock_verification_never_accepts`, `unit/test_verification_review.py::test_mock_review_never_accepts`; diagrams/costs NOT_IMPLEMENTED | control_center, cost |
| 3.28 | Change proposal ditolak atau rollback dilakukan | NOT_IMPLEMENTED | history keeps reason/evidence | proposals |

## Summary

| Status | Rows |
|---|---|
| ACCEPTANCE (fully or partially in the trusted suite) | 1.3, 1.5, 1.6, 1.9, 1.10, 1.12, 1.14, 1.23, 1.24, 1.25, 1.37, 2.10, 3.13, 3.14 |
| UNIT/INTEGRATION only | 1.4, 1.13, 1.16, 1.22, 1.28, 1.30, 1.31, 1.33, 1.34, 1.35, 1.40, 2.5, 2.6, 2.11, 2.13, 2.14, 3.6, 3.9, 3.18, 3.19, 3.21, 3.27 |
| BLOCKED parts (external capability) | 1.13 (real worker sandbox), 1.33 (GitHub plan enforcement), 1.35 (deployment target) |
| NOT_IMPLEMENTED | all remaining rows (owners listed) |

Rows marked "(partial)" are not fully covered; the uncovered part is named in the row. When a module
lands its implementation, add a trusted test here and update the row — never mark a row ACCEPTANCE
on the basis of a design or a mock.
