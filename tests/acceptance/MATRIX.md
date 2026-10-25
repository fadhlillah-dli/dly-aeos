# §50 acceptance matrix → trusted acceptance suite

Every row of master §50 (all three tables, 82 rows) is listed exactly once, with the trusted tests
that prove it, the status and the evidence level. Test ids are `file::test` relative to
`tests/acceptance/`.

## Summary (integration run 2026-10-18, review 2026-10-19, wave-2 integration `integration/wave2` 2026-10-19)

| Status | Rows | Count |
|---|---|---|
| **PASS** (every part of the expected result proven) | all rows not listed below | 76 |
| **PASS + XFAIL-BUG** (proven parts PASS; a named product bug is a strict xfail) | — | 0 |
| **PASS + BLOCKED** (local part PASS; the rest needs an external capability) | 1.12, 1.13, 1.19, 1.33, 1.34, 1.35 | 6 |
| **Total** | | **82** |

No strict xfails remain (BUG_PII and BUG_PIN_REDECLARED were fixed during integration); BLOCKED tests are listed under
"Blocked" at the end. A strict xfail turns into a hard failure the moment the bug is fixed, so the
row must then be re-marked PASS. Eight former strict xfails (5 bugs) were fixed by the wave-2 seams and
are now PASS (see "Fixed by the wave-2 seams").

### Status and evidence vocabulary

* **PASS** — the test drives the integrated control plane (HTTP API and module services) with real
  planning, policy/decision registry, interlock, scheduler, worker runtime, independent verifier,
  publisher, inventory, research/proposals and operations code. Only external services/tools are
  faked: the host probe boundary (`fakes.FakeHost`), the secret backend (`fakes.ContractSecretStore`,
  which still enforces the real purpose/project-scope contract), the Claude Code process
  (`fakes.FakeClaude` at `bridge.run_process`), and Google/GitHub/OpenRouter/Confluence HTTP via
  `respx`. Evidence level **E2 (controlled)**: real code paths on this machine against fakes of the
  external parties; no production, tenant or provider evidence (that would be E3+).
* **Controlled preconditions** (labelled in the code): `world.doctor_ready` writes Doctor results
  for one capability (`observed_evidence.fixture = acceptance`, 1 h TTL) where a row is about
  scheduler/runtime semantics and the host cannot make the capability READY for real (no Docker
  containment). Like a real Doctor run it is written AFTER the owner decisions and route changes it
  depends on: recording a ROUTE_ACTIVATION/EXCEPTION/WRITE_ENABLEMENT decision marks the dependent
  results STALE (§08 invalidation), and STALE never counts. It does not replace a human decision:
  CAN_RUN_WORKER reads the ADR-0051 sandbox requirement live, so the owner's process-sandbox EXCEPTION
  is always recorded through the decision registry. `suite.trusted_controller_root` points the
  verifier at a tiny trusted grader (running this suite inside the verifier would recurse);
  `world.fake_opencode` reports the external OpenCode binary.
* **XFAIL-BUG** — `pytest.mark.xfail(strict=True, reason="BUG: ...")`. Each was confirmed to fail at
  the bug assertion (`--runxfail`), never earlier.
* **BLOCKED-<reason>** — `@pytest.mark.integration` + `pytest.skip("BLOCKED-...")`; deselected by the
  verifier's `-m "not integration and not network"`. Never counted as a pass.

## Table 1 — Acceptance matrix untuk implementasi

| # | §50 row | Test id(s) | Status | Evidence |
|---|---|---|---|---|
| 1.1 | Dependency inti belum terpasang | `test_bootstrap_readiness.py::test_missing_core_dependency_is_explained_by_the_bootstrap_cli`, `::test_bootstrap_component_list_names_missing_tools_with_install_hints` | PASS | E2 |
| 1.2 | Tool opsional atau Office belum ready | `test_bootstrap_readiness.py::test_optional_tools_and_office_missing_keep_dashboard_and_independent_features`, `::test_missing_module_degrades_one_section_not_the_dashboard` | PASS | E2 |
| 1.3 | Port lokal konflik | `test_runtime_binding.py::test_port_conflict_is_explained_not_rebound`, `::test_only_loopback_binding`, `::test_default_bind_is_loopback` | PASS | E2 |
| 1.4 | Doctor hanya berhasil connect | `test_bootstrap_readiness.py::test_connected_github_probe_is_not_verified_permission` | PASS | E2 |
| 1.5 | Config/model/provider berubah setelah doctor | `test_bootstrap_readiness.py::test_binary_change_after_doctor_marks_checks_stale_and_recheck_runs`, `::test_route_configuration_change_invalidates_route_checks`; `test_evidence_honesty.py::test_acceptance_validity_is_bound_to_identity` | PASS | E2 |
| 1.6 | Candidate DLY AEOS mengubah aktif policy/verifier/controller | `test_self_build.py::test_candidate_changing_active_policy_or_verifier_is_quarantined_until_human_review` (verifier QUARANTINED, review refused `VERIFICATION_NOT_PASSED`, and the broker refuses the push `QUARANTINED` even with a GIT_BRANCH grant until a human special-review release); `test_evidence_honesty.py::test_grader_policy_pipeline_changes_never_pass_scope` | PASS | E2 |
| 1.7 | Upgrade controller gagal | `test_operations.py::test_failed_controller_upgrade_keeps_the_old_release_and_recoverable_data` | PASS | E2 |
| 1.8 | Source Office muncul dalam platform personal working tree | `test_untrusted_input.py::test_office_source_in_the_personal_platform_tree_is_refused`, `::test_office_origin_checkout_in_a_personal_project_is_never_read` | PASS | E2 |
| 1.9 | Submit prompt tanpa domain | `test_untrusted_input.py::test_prompt_without_domain_is_refused_before_any_model_call`; `test_domain_scope.py::test_domain_scoped_reads_require_explicit_domain` | PASS | E2 |
| 1.10 | Prompt memerintahkan OFFICE menjadi PERSONAL | `test_untrusted_input.py::test_prompt_ordering_office_to_personal_changes_nothing`; `test_domain_scope.py::test_project_domain_is_immutable` | PASS | E2 |
| 1.11 | Lampiran berisi instruksi mengirim source ke GitHub personal | `test_untrusted_input.py::test_attachment_instructions_grant_no_authorisation_or_tool_permission` | PASS | E2 |
| 1.12 | File path traversal/symlink keluar sandbox | `test_containment.py::test_task_write_scope_that_leaves_the_workspace_never_becomes_a_task`, `::test_candidate_symlink_and_traversal_are_never_materialised_or_accepted`, `::test_upload_archive_with_traversal_or_symlink_is_rejected`; `test_runtime_binding.py::test_path_traversal_segments_rejected`, `::test_managed_paths_cannot_escape`; BLOCKED: `test_containment.py::test_container_sandbox_blocks_filesystem_and_egress` (a worker process reading through an absolute path or a symlink outside its worktree is not prevented by the OS in the process sandbox; the container suite implemented 2026-10-19 runs on a host with Docker + the worker image and skips otherwise) | PASS + BLOCKED-docker-containment | E2 (scope/materialisation/upload paths); worker-runtime read containment not proven on this host |
| 1.13 | Worker membaca vault/SSH/native Claude credential | PASS: `test_containment.py::test_worker_environment_and_tools_never_carry_vault_ssh_or_claude_credentials`, `::test_process_sandbox_containment_is_reported_honestly`, `::test_doctor_containment_check_reflects_the_runtime_report` (was XFAIL BUG_CONTAINMENT_REPORT, fixed by W3); BLOCKED: `::test_container_sandbox_blocks_filesystem_and_egress` (container suite implemented 2026-10-19, runs on a host with Docker + the worker image; skips otherwise) | PASS + BLOCKED-docker-containment | E2 (env/tool/egress allow-lists; no shell under the process sandbox); FS containment not proven on this host |
| 1.14 | VIEWER memanggil write endpoint langsung | `test_auth_boundary.py::test_viewer_is_denied_on_every_write_route` | PASS | E2 |
| 1.15 | Bootstrap belum selesai tetapi publish diminta | `test_publication.py::test_publish_before_bootstrap_is_refused` | PASS | E2 |
| 1.16 | Approval PlanVersion lama | `test_publication.py::test_approving_an_old_plan_version_is_refused_and_latest_reported` (refused, latest reported, nothing recorded as a decision, each refused attempt kept as a DENIED audit row) | PASS | E2 |
| 1.17 | Revisi baru muncul selama publish | `test_publication.py::test_publication_binds_the_approved_version_never_latest`, `::test_new_revision_during_a_running_publication_keeps_the_approved_snapshot` (was XFAIL BUG_BINDING, fixed by W1) | PASS | E2 |
| 1.18 | Google Docs direvisi manusia | adapter (respx Google): `test_publication.py::test_google_docs_human_revision_is_a_conflict_never_an_overwrite`; end to end: `::test_human_edit_during_update_is_kept_and_handed_to_review` (was XFAIL BUG_BINDING, fixed by W1) | PASS | E2 |
| 1.19 | Tenant tidak mendukung folder Confluence yang wajib | PASS (respx Confluence + OFFICE default deny): `test_publication.py::test_confluence_parent_without_folder_support_blocks_without_changing_structure`; BLOCKED: `::test_confluence_folder_support_on_a_real_tenant` | PASS + BLOCKED-real-atlassian-tenant | E2 |
| 1.20 | Provider timeout setelah create resource | `test_publication.py::test_provider_timeout_after_create_is_reconciled_before_retry` (was XFAIL BUG_BINDING, fixed by W1) | PASS | E2 |
| 1.21 | Salah satu target publikasi gagal | `test_publication.py::test_one_failing_target_is_partial_and_successes_are_kept` (was XFAIL BUG_BINDING, fixed by W1) | PASS | E2 |
| 1.22 | Jira/GitHub issue Done tanpa evidence runtime | `test_workforce.py::test_issue_done_without_runtime_evidence_does_not_unlock_dependency` | PASS | E2 |
| 1.23 | Worker exit 0 / menulis "all tests pass" | PASS: `test_self_build.py::test_clean_candidate_is_accepted_only_after_verifier_and_review`, `::test_review_without_proven_bridge_never_passes_automatically`; `test_workforce.py::test_worker_exit_zero_and_all_tests_pass_stays_review_not_done`; `test_evidence_honesty.py::test_worker_evidence_is_an_untrusted_claim`, `::test_worker_exit_zero_yields_no_acceptance_and_no_pass`; native review gate: `test_self_build.py::test_passing_native_capability_test_makes_max_review_ready` (was XFAIL BUG_REVIEW_CAPABILITY, fixed by W3) | PASS | E2 (native gate proven against the fake official client; end-to-end reviews via the §12 manual package) |
| 1.24 | Worker mengubah grader/policy/pipeline di luar kontrak | `test_self_build.py::test_candidate_rewriting_grader_or_pipeline_cannot_fake_its_pass` (first line: the runtime scope check fails/quarantines the attempt; second line proven on its own with that check bypassed: the trusted grader runs, the shadowed test runner is refused) | PASS | E2 |
| 1.25 | PR baru mengubah head atau base terkait | acceptance side: `test_self_build.py::test_changed_grader_or_moved_head_makes_acceptance_stale`; `test_delivery.py::test_unaccepted_task_cannot_be_prepared_as_a_pr`; PR sync: `test_delivery.py::test_pr_with_new_commits_makes_the_acceptance_stale` (was XFAIL BUG_DELIVERY, fixed by W2; the owner's plan approval names the repository, which is what authorises `create_pr`) | PASS | E2 (respx GitHub, faked mirror push) |
| 1.26 | Worker A dan B menulis shared schema yang sama | `test_workforce.py::test_parallel_tasks_with_overlapping_write_sets_are_serialised` | PASS | E2 |
| 1.27 | Controller crash dan worker lama masih aktif | `test_workforce.py::test_controller_crash_with_old_worker_alive_never_starts_a_duplicate` | PASS | E2 |
| 1.28 | Cancel saat ada child process | `test_workforce.py::test_cancel_from_dashboard_kills_the_tree_revokes_and_releases` | PASS | E2 (real process tree) |
| 1.29 | Free quota habis / 429 | `test_routing_cost.py::test_free_quota_429_backs_off_and_never_falls_back_to_paid` | PASS | E2 |
| 1.30 | Fable Max quota habis | `test_routing_cost.py::test_fable_quota_exhausted_makes_review_wait_and_never_skips_the_gate` | PASS | E2 |
| 1.31 | Model alias/provider route berubah | `test_routing_cost.py::test_supervisor_alias_resolving_to_a_new_model_is_a_new_configuration`, `::test_worker_route_changed_by_the_provider_loses_its_approval` | PASS | E2 |
| 1.32 | Budget DB/gateway tidak sehat | `test_routing_cost.py::test_untrusted_budget_state_refuses_admission`, `::test_missing_cost_module_refuses_admission` | PASS | E2 |
| 1.33 | Agent mencoba direct push main/merge API | PASS (broker push to a local bare remote, refspec/branch refusals, no merge operation anywhere, interlock HUMAN_ONLY): `test_delivery.py::test_direct_push_to_main_or_merge_is_refused_by_the_broker_boundary`; BLOCKED: `::test_provider_enforces_human_only_merge_on_the_real_repository` | PASS + BLOCKED-real-github-enforcement | E2 |
| 1.34 | PR code mencoba membaca production secret | PASS: `test_containment.py::test_pr_code_under_the_verifier_sees_no_production_secret`, `::test_pr_ci_workflows_get_no_secrets_and_a_read_only_token`; BLOCKED: `::test_github_hosted_pr_run_has_no_secret_access` | PASS + BLOCKED-real-github | E2 |
| 1.35 | Deploy sukses tetapi fungsi utama gagal | PASS: `test_self_build.py::test_production_claim_without_production_evidence_is_never_pass_production`; BLOCKED: `::test_post_deploy_failure_triggers_incident_and_rollback` | PASS + BLOCKED-no-deployment-target | E2 |
| 1.36 | Restore backup belum pernah diuji | `test_operations.py::test_untested_backup_is_never_reported_as_recovery_ready` | PASS | E2 (real backup + restore test) |
| 1.37 | Trace berisi secret/PII | PASS (secrets): `test_operations.py::test_trace_secret_is_redacted_before_storage_and_export`; `test_runtime_binding.py::test_secrets_redacted_before_persistence`; PASS (PII minimisation, aeos.core.pii): `test_operations.py::test_trace_pii_is_redacted_or_limited_before_storage_and_export` | PASS | E2 |
| 1.38 | Memory lama/cross-project mencoba menjadi kebijakan | `test_governance.py::test_memory_only_from_accepted_facts_never_policy_never_cross_scope` | PASS | E2 |
| 1.39 | Model/prompt/tool/policy baru | `test_governance.py::test_new_model_or_tool_needs_controlled_eval_version_decision_and_rollback_before_promotion` | PASS | E2 (trial evidence from the real verifier) |
| 1.40 | Klaim compliance dari template/scanner hijau | `test_control_center.py::test_compliance_is_never_claimed_from_templates_or_green_scanners` | PASS | E2 |

## Table 2 — Organisasi dan Workforce Console

| # | §50 row | Test id(s) | Status | Evidence |
|---|---|---|---|---|
| 2.1 | Role catalog berisi banyak jabatan, belum ada task | `test_workforce.py::test_role_catalog_without_tasks_makes_no_llm_run_or_cost` | PASS | E2 |
| 2.2 | Tugas kecil tidak membutuhkan spesialis | `test_workforce.py::test_small_task_uses_a_single_implementer` | PASS | E2 |
| 2.3 | Task dinilai dapat paralel tetapi write sets overlap | `test_workforce.py::test_parallel_tasks_with_overlapping_write_sets_are_serialised` | PASS | E2 |
| 2.4 | Agent berposisi "CTO/manager" meminta privilege anak | `test_workforce.py::test_manager_title_grants_no_privilege` | PASS | E2 |
| 2.5 | Pesan antaragent mengaku sudah mendapat izin owner | `test_workforce.py::test_agent_message_claiming_owner_approval_is_data` | PASS | E2 |
| 2.6 | Worker selesai tetapi verifier gagal | `test_self_build.py::test_worker_done_but_verifier_failed_is_a_visible_failure`, `::test_workforce_console_shows_the_verifier_result` (was XFAIL BUG_VERIFIER_STATUS, fixed by W2); `test_workforce.py::test_worker_done_but_verifier_failed_is_shown_as_failure` | PASS | E2 |
| 2.7 | Lease lama mengirim event setelah retry/reassign | `test_workforce.py::test_stale_lease_events_after_reassign_never_touch_the_new_attempt` | PASS | E2 |
| 2.8 | SSE/stream putus | `test_control_center.py::test_broken_stream_resumes_from_the_cursor_and_is_never_shown_live` (real SSE endpoint over raw ASGI: resume by Last-Event-ID, ordered/de-duplicated, deterministic replay, reconnects write no events/usage/observations/runs, no other-domain events, last observed time, UNKNOWN when nothing recorded). The browser rendering of STALE is covered by `frontend/src/pages/overview/overview.test.tsx` "shows STALE with last observed time when disconnected" (vitest, outside the trusted suite) | PASS | E2 (backend) |
| 2.9 | Cancel dari dashboard | `test_workforce.py::test_cancel_from_dashboard_kills_the_tree_revokes_and_releases` | PASS | E2 |
| 2.10 | Dashboard Personal mencoba run/evidence Office | `test_workforce.py::test_personal_dashboard_cannot_reach_office_runs_or_evidence`; `test_domain_scope.py::test_personal_session_cannot_see_office_records` | PASS | E2 |
| 2.11 | Fixture roster/run ditampilkan saat belum ada runtime | `test_workforce.py::test_demo_roster_and_runs_are_labelled_and_never_counted` | PASS | E2 |
| 2.12 | Role/model/mode diganti | `test_workforce.py::test_role_model_change_is_versioned_and_history_is_unchanged` | PASS | E2 |
| 2.13 | Candidate skill meminta auto-update policy | `test_self_build.py::test_candidate_changing_active_policy_or_verifier_is_quarantined_until_human_review` (indirect: a candidate that changes active policy is quarantined). Direct proof of the row, outside the trusted suite: `tests/unit/test_workforce_skill_import.py::test_pack_requesting_auto_update_or_hooks_is_refused` (a skill pack requesting auto-update or hooks is refused at import) | PASS | E2 |
| 2.14 | Controller membangun versi AEOS baru | `test_self_build.py::test_candidate_rewriting_grader_or_pipeline_cannot_fake_its_pass` (indirect: the candidate cannot fake its own pass). Direct proof of the row, outside the trusted suite: `tests/unit/test_verification_scope.py::test_capability_audit_dashboard_and_master_paths_are_protected` (capability/audit/dashboard/master paths of a self-build candidate are protected) | PASS | E2 |

## Table 3 — Transparansi tools/repo dan perubahan arsitektur

| # | §50 row | Test id(s) | Status | Evidence |
|---|---|---|---|---|
| 3.1 | Katalog blueprint berisi banyak tool tetapi host belum diprobe | `test_transparency.py::test_unprobed_catalog_is_design_and_unknown_never_installed_or_in_use` | PASS | E2 |
| 3.2 | Tool ditemukan dalam README atau lockfile | `test_transparency.py::test_readme_and_lockfile_mentions_are_declarations_and_unregistered_finds_are_never_adopted` | PASS | E2 |
| 3.3 | Tool installed dan enabled tanpa run yang teramati | `test_transparency.py::test_installed_and_enabled_without_a_run_is_not_in_use` | PASS | E2 |
| 3.4 | Model request sudah selesai | `test_routing_cost.py::test_finished_model_request_is_last_used_not_an_active_session` | PASS | E2 |
| 3.5 | Probe connectivity berhasil tetapi functional check gagal | `test_bootstrap_readiness.py::test_connected_but_functional_check_failed_is_not_ready` | PASS | E2 |
| 3.6 | Binary/model route/config berganti dari approved pin | PASS: `test_transparency.py::test_binary_drift_from_the_approved_pin_is_visible_and_invalidates_readiness`; `test_bootstrap_readiness.py::test_route_configuration_change_invalidates_route_checks`; PASS: `test_transparency.py::test_redeclared_pin_that_the_binary_no_longer_matches_invalidates_readiness` (doctor compares with the approved manifest pin; manifest change invalidates worker.opencode) | PASS | E2 |
| 3.7 | Upstream release baru terdeteksi | `test_governance.py::test_upstream_release_is_a_notification_never_an_install_route_or_policy_change` | PASS | E2 (respx GitHub REST) |
| 3.8 | Source repo rename/redirect/fork/publisher berubah | `test_governance.py::test_renamed_redirected_or_forked_source_does_not_inherit_trust` | PASS | E2 (respx GitHub REST) |
| 3.9 | Nama repo ambigu atau sumber tidak terverifikasi | `test_governance.py::test_ambiguous_or_unverified_source_is_unverified_and_nothing_is_invented` | PASS | E2 |
| 3.10 | Komponen ditemukan di managed scope tanpa registrasi | `test_transparency.py::test_readme_and_lockfile_mentions_are_declarations_and_unregistered_finds_are_never_adopted` | PASS | E2 |
| 3.11 | Worker memanggil binary/tool/endpoint tidak admitted | `test_containment.py::test_worker_calls_outside_its_capability_are_refused` | PASS | E2 |
| 3.12 | Inventory/stream gagal atau offline | `test_transparency.py::test_offline_inventory_shows_the_last_snapshot_and_stale_never_green`; reconnect/dedup: `test_control_center.py::test_broken_stream_resumes_from_the_cursor_and_is_never_shown_live` | PASS | E2 |
| 3.13 | Viewer mengakses detail/search/export inventory Office | `test_transparency.py::test_viewer_cannot_reach_office_inventory_detail_search_export_or_counts` (detail/relations/blast radius 404, search/export/map/coverage without OFFICE ids or counts, OFFICE events and the stored OFFICE credential invisible, a PERSONAL-scoped viewer cannot switch to OFFICE; control: the same records are served in an owner's OFFICE session) | PASS | E2 |
| 3.14 | Source/tool output memasukkan secret/instruksi berbahaya | `test_untrusted_input.py::test_model_output_claiming_approval_or_carrying_a_secret_stays_data`, `::test_repository_file_with_secret_and_instructions_is_redacted_untrusted_context` | PASS | E2 |
| 3.15 | Pengguna hanya meminta compare/review URL | `test_governance.py::test_compare_request_never_installs_activates_raises_budget_or_sends_data` | PASS | E2 |
| 3.16 | Kandidat lebih cepat/murah tetapi gagal authority/security gate | `test_governance.py::test_faster_cheaper_candidate_failing_a_hard_gate_is_excluded_not_promoted` | PASS | E2 |
| 3.17 | Trial disetujui, promotion belum disetujui | `test_governance.py::test_trial_approval_is_not_promotion_and_the_baseline_stays_active` | PASS | E2 |
| 3.18 | Baseline/candidate/target berubah setelah approval | `test_governance.py::test_binding_is_refused_when_baseline_or_candidate_changes_after_approval` | PASS | E2 |
| 3.19 | Replace mengubah caller/schema/data/CI | `test_governance.py::test_replace_review_requires_impact_consumers_migration_and_rollback` | PASS | E2 |
| 3.20 | Dua orchestration tools hendak menjadi scheduler aktif | `test_governance.py::test_a_second_orchestrator_never_becomes_an_active_scheduler` | PASS | E2 |
| 3.21 | Migration gagal atau rollback data tidak mungkin | `test_operations.py::test_code_rollback_is_refused_when_data_cannot_roll_back` | PASS | E2 (real release/rollback) |
| 3.22 | Canary bukan metode aman bagi komponen | `test_governance.py::test_unsafe_canary_is_replaced_by_sandbox_staging_and_an_authorised_cutover` | PASS | E2 |
| 3.23 | Runtime aktif tetapi external docs publication conflict | `test_publication.py::test_publication_status_keeps_docs_published_separate_from_active_and_verified`, `::test_one_failing_target_is_partial_and_successes_are_kept` (was XFAIL BUG_BINDING, fixed by W1) | PASS | E2 |
| 3.24 | GDocs/Confluence diedit manusia selama update | adapter: `test_publication.py::test_google_docs_human_revision_is_a_conflict_never_an_overwrite`; end to end: `::test_human_edit_during_update_is_kept_and_handed_to_review` (was XFAIL BUG_BINDING, fixed by W1) | PASS | E2 |
| 3.25 | Snapshot export dibuka setelah runtime berubah | `test_operations.py::test_export_snapshot_is_scoped_timed_hashed_and_never_live` | PASS | E2 |
| 3.26 | Nama master tetap Final setelah perubahan sah | `test_control_center.py::test_master_keeps_its_final_name_and_history_after_lawful_changes` (git history is asserted in a checkout; inside the verifier's exported candidate snapshot, which has no `.git`, only the file-level checks apply) | PASS | E2 (repository + git history) |
| 3.27 | Mock diagram, roster, costs atau tests ditampilkan | `test_control_center.py::test_mock_costs_runs_diagrams_and_tests_are_labelled_and_never_counted`; `test_workforce.py::test_demo_roster_and_runs_are_labelled_and_never_counted`; `test_evidence_honesty.py::test_mock_acceptance_is_never_valid` | PASS | E2 |
| 3.28 | Change proposal ditolak atau rollback dilakukan | `test_governance.py::test_rejected_proposal_keeps_reason_and_history_and_is_never_success`, `::test_rolled_back_change_keeps_its_history_and_evidence_and_is_never_success` | PASS | E2 |

## Seam bugs (formerly strict xfail, all FIXED; kept for history)

| Id | Seam | Rows | Tests |
|---|---|---|---|
| BUG_PIN_REDECLARED (FIXED in integration) | doctor: a re-declared approved pin invalidates only `manifest`-invalidator checks; `worker.opencode` accepts any version; HIGH version drift is not a CAN_RUN_WORKER input → READY while off-pin (re-confirmed on `integration/wave2` with `--runxfail`: fails at the final `!= READY` assertion) | 3.6 | `test_transparency.py::test_redeclared_pin_that_the_binary_no_longer_matches_invalidates_readiness` |
| BUG_PII (FIXED in integration) | core redaction: no PII patterns/policy; PII in traces is stored and exported verbatim (re-confirmed on `integration/wave2`) | 1.37 | `test_operations.py::test_trace_pii_is_redacted_or_limited_before_storage_and_export` |

### Fixed by the wave-2 seams (former strict xfails, now PASS)

| Id | Fixed by | Rows | Tests |
|---|---|---|---|
| BUG_BINDING | W1 planning/publication seam: one provider vocabulary (`aeos.portfolio.providers`), targets `{provider, root_id}`, publisher writes bound to the plan APPROVAL | 1.17, 1.18, 1.20, 1.21, 3.23, 3.24 | `test_publication.py` (4 tests) |
| BUG_DELIVERY | W2 execution/delivery seam: `prepare_pr` binds the PR to the plan APPROVAL (`create_pr` + repository target) and issues the GIT_BRANCH/PR_CREATE grants | 1.25 | `test_delivery.py::test_pr_with_new_commits_makes_the_acceptance_stale` |
| BUG_VERIFIER_STATUS | W2: verifier/workforce run status vocabulary reconciled | 2.6 | `test_self_build.py::test_workforce_console_shows_the_verifier_result` |
| BUG_REVIEW_CAPABILITY | W3 doctor readiness seam: `claude.capability_test` reads the bridge's `status`/`mode` | 1.23 | `test_self_build.py::test_passing_native_capability_test_makes_max_review_ready` |
| BUG_CONTAINMENT_REPORT | W3: `containment.report` reads the runtime's `check`/`status` items | 1.13 | `test_containment.py::test_doctor_containment_check_reflects_the_runtime_report` |

## Blocked (external capability; `@pytest.mark.integration`, skipped with the reason)

| Reason | Rows | Test |
|---|---|---|
| BLOCKED-docker-containment | 1.12, 1.13 | `test_containment.py::test_container_sandbox_blocks_filesystem_and_egress` |
| BLOCKED-real-github | 1.34 | `test_containment.py::test_github_hosted_pr_run_has_no_secret_access` |
| BLOCKED-real-github-enforcement | 1.33 | `test_delivery.py::test_provider_enforces_human_only_merge_on_the_real_repository` |
| BLOCKED-real-atlassian-tenant | 1.19 | `test_publication.py::test_confluence_folder_support_on_a_real_tenant` |
| BLOCKED-no-deployment-target | 1.35 | `test_self_build.py::test_post_deploy_failure_triggers_incident_and_rollback` |

Not needed by any row on this host: real OpenBao (the secret backend is the contract-enforcing
fake) and macOS (no row is platform-specific beyond the Windows command-line bound, which runs here).
This suite was run on Windows 11 only; macOS/Linux results come from CI and are not proven by these
runs.

## Review 2026-10-19 (adversarial review of the integration)

* **The suite must also work where the verifier runs it** — a copy of this directory run against an
  exported candidate snapshot that has no `.git`, with a scrubbed environment (see the conftest
  docstring). 3.26 called `git log` with `check=True` on `repo_root()`. Inside the snapshot that fails,
  so every platform self-build verification would have FAILED for a reason that has nothing to do with
  the candidate. The git-history assertions now run only when `repo_root()` is the top of a git
  checkout (developer run, CI); in the snapshot only the file-level checks apply. A simulated verifier
  run (snapshot copy + `copy_trusted_suite` + `scrubbed_env` + the verifier's ini) passed 3.26. Under
  that simulation's longer base path, eleven self-build tests hit the Windows MAX_PATH limit (the real
  verifier work dir is about 60 characters shorter). That part is NOT-PROVEN.
* **Every child process in the suite is bounded** (`world.GIT_TIMEOUT`, hooks disabled for test
  commits). The cancel test now checks the grandchild by pid and create time with a bounded wait,
  instead of a check that could race with process exit.
* **The self-build world mirrors the runtime's own quarantine bookkeeping**
  (`worker_runtime.scope.check_scope` with the platform protected paths, as `executor._finalize`
  does). Before this, a candidate that touched `policy/**` or the verifier was never marked
  `quarantined`, so the 1.6 boundary "patch stays candidate" was only proven by an unrelated
  `CAPABILITY_MISSING`. It is now proven by the broker's `QUARANTINED` refusal while a GIT_BRANCH grant
  is held. 1.24 bypasses that first line explicitly to prove the verifier on its own.
* **Assertions that passed for the wrong reason were tightened**: 2.4 (`DECISION_ACTOR_NOT_HUMAN` for
  agent/title actors, not "any exception mentioning actor"), 2.11 (the cost check read a key that
  does not exist), 3.27 (an empty map passed vacuously; the DESIGN lens label is now asserted), 1.6
  (the exact refusal code and verifier status).
* **Coverage added**: 1.16 (each refused stale approval is kept as a DENIED audit row; product fix
  below), 2.8/3.12 (reconnects write no events, usage, observations or runs; replay is
  deterministic), 3.13 (OFFICE events, the stored OFFICE credential, PERSONAL-scoped viewer, with an
  owner-in-OFFICE control showing that the hidden records exist).
* **Product fix**: `planning.service.decide` recorded the DENIED audit for a stale approval, but the
  API rolled it back together with the failed request. The denial is now committed before the
  conflict is raised, the same pattern `office_gate` uses.

## Wave-2 integration 2026-10-19 (`integration/wave2`: W1 planning/publication, W2 execution/delivery, W3 doctor readiness, W4 UI e2e, W5 this suite)

The fixtures were reconciled with the contracts the other seams changed; no assertion was loosened.

* **Doctor invalidation (W3)**: an owner ROUTE_ACTIVATION/EXCEPTION/WRITE_ENABLEMENT decision marks the
  dependent Doctor results STALE. `world.worker_ready`, `test_transparency._enabled_opencode` and the
  1.25 PR test wrote the `doctor_ready` controlled precondition before those decisions, so it was
  (correctly) stale; it is now written after them.
* **Live sandbox requirement (W3)**: CAN_RUN_WORKER evaluates ADR-0051 live, so 3.6 records the owner's
  process-sandbox EXCEPTION through the registry instead of relying on stored check results alone.
* **Provider vocabulary (W1)**: publication targets are exactly `{provider, root_id}` (`github-issues`
  root id = `owner/repo`); Google Docs writes are `google-drive` operations, so the world enables and
  approves `google-drive` + `github-issues` only. The Doctor's `adapter.write_enablement` now takes the
  PERSONAL publication providers from `aeos.portfolio.providers` (it still required a `google-docs`
  enablement that no longer exists). Legacy-input tests elsewhere still prove normalisation/refusal.
* **Delivery binding (W2)**: a PR is authorised by the plan APPROVAL whose operations include
  `create_pr` and whose targets name the repository; the 1.25 PR test approves with
  `{"provider": "github-repo", "repo": owner/name}`.
* Eight strict xfails XPASSed and were re-marked PASS (table above). The two strict xfails that
  remained at that point (BUG_PII 1.37, BUG_PIN_REDECLARED 3.6) were fixed later in the integration and
  are PASS too; no strict xfail remains.

## How to run

```
python -m pytest tests/acceptance -m "not integration and not network"
python -m pytest tests/acceptance -m "not integration and not network" --runxfail   # each xfail must fail at its bug
```

Never mark a row PASS on the basis of a design, a mock or an xfail; when a seam bug is fixed the
strict xfail fails (XPASS) and the row is re-marked here.
