### Fixed

- **A process exiting anywhere on the host no longer quarantines a clean verification.** The POSIX kill-group sweep
  (`aeos.verification.procgroup`) reads the environment of every process on the host. On macOS psutil raises
  `SystemError` when that read meets a process the kernel is tearing down (`sysctl(KERN_PROCARGS2)` fails with
  errno 0). The error escaped the sweep, the step's group was reported as unreadable (fail closed), and a step
  without a single descendant, such as the import preflight, was QUARANTINED with "left processes running after its
  step". This happened more often on busier hosts, because more processes were exiting during each sweep. It is the
  flaky-test class of lesson L15: it was misread as a timing problem and re-run. Each process is now read on its
  own and judged by its state, not by the error. A process that has finished exiting (gone, or a zombie) is not a
  member. A process read normally on a later attempt is judged like any other. A process still alive and still
  unreadable after `UNREADABLE_RECHECK_SECONDS` is a member (fail closed). Each sweep builds fresh process objects
  from the pid list instead of `psutil.process_iter`'s cache, which can keep the identity of an older process that
  used the same pid. Evidence: E2 unit run on macOS arm64. A deterministic regression test (the read of an unrelated
  process fails while that process is torn down) is red on the old code and green on the fix. A fail-closed control
  (a detached descendant whose every read fails is still a survivor and is killed) turns red when the fix is changed
  to skip unreadable processes. Under synthetic load on the same Mac (10 CPU burners plus 8 process-churn loops), the
  six tests that had flaked ran 30 times each: 174 of 180 passed before the fix (all 6 failures were "group
  unreadable (SystemError)"), 180 of 180 passed after it. NOT-PROVEN on Windows (that path uses the Job Object; its marker-sweep fallback shares the new per-process read
  but was not run there) and on Intel macOS until CI runs there.
