# Control Center UI evidence (real browser)

Captured by `tests/e2e/test_ui_smoke.py` (`AEOS_UI_EVIDENCE=1`) against the real server
(`uvicorn aeos.api.app:create_app --factory`) on 127.0.0.1 with a fresh data directory.

| Field | Value |
|---|---|
| Commit | `c07370dbde71d05300aac6c8b9eff7d6b7d6c5a9` + uncommitted working-tree changes (the commit that adds these files) |
| Date | 2026-10-18 17:53 UTC |
| OS | Windows 11 (10.0.26200) |
| Browser | Chromium 156.0.8078.4 (Playwright, headless) |
| Viewports | 1440x900 (desktop) and 390x844 (mobile): full page, cut at 6000 px; dark (`data-theme="dark"`, 1440x900): first screen. JPEG q60; lossless full-page PNGs of every route stay in the test's temp dir |
| Data | seeded **DESIGN** catalog (`aeos seed`) + read-only `aeos inventory reconcile` probes with claude/opencode/bao deliberately absent + labelled **DEMO** runs (`aeos work demo-fixtures`). **Not live usage**: no worker ran, no model was called, no external account was connected. |

Evidence level: E1 visual smoke (pages render without page/console errors or 5xx; labels visible).
It does not prove functional readiness of any component; DESIGN/DEMO rows remain DESIGN/DEMO.

Note: these images were captured before the W4 review commit. The only visible change since then is
the header data-mode label for `MIXED`, which now reads "LIVE + MOCK/DEMO" (the images show
"LIVE + DEMO"); the projection does not tell MOCK and DEMO rows apart. Re-run with
`AEOS_UI_EVIDENCE=1` to refresh the set.

| Page | Route | Desktop | Mobile | Dark |
|---|---|---|---|---|
| ringkasan | `/` | [1440](ringkasan-1440.jpg) | [390](ringkasan-390.jpg) | [dark](ringkasan-dark.jpg) |
| kesiapan | `/readiness` | [1440](kesiapan-1440.jpg) | [390](kesiapan-390.jpg) | [dark](kesiapan-dark.jpg) |
| peta-sistem | `/system-map` | [1440](peta-sistem-1440.jpg) | [390](peta-sistem-390.jpg) | [dark](peta-sistem-dark.jpg) |
| tools-repos | `/tools` | [1440](tools-repos-1440.jpg) | [390](tools-repos-390.jpg) | [dark](tools-repos-dark.jpg) |
| model-rute | `/models` | [1440](model-rute-1440.jpg) | [390](model-rute-390.jpg) | [dark](model-rute-dark.jpg) |
| pekerjaan-live | `/work` | [1440](pekerjaan-live-1440.jpg) | [390](pekerjaan-live-390.jpg) | [dark](pekerjaan-live-dark.jpg) |
| organisasi | `/organization` | [1440](organisasi-1440.jpg) | [390](organisasi-390.jpg) | [dark](organisasi-dark.jpg) |
| rencana | `/plans` | [1440](rencana-1440.jpg) | [390](rencana-390.jpg) | [dark](rencana-dark.jpg) |
| usulan-upgrade | `/proposals` | [1440](usulan-upgrade-1440.jpg) | [390](usulan-upgrade-390.jpg) | [dark](usulan-upgrade-dark.jpg) |
| riwayat | `/history` | [1440](riwayat-1440.jpg) | [390](riwayat-390.jpg) | [dark](riwayat-dark.jpg) |

## Reproduce

The `[dev]` extra includes playwright. The first pytest run writes screenshots to a temp dir; the second
(with `AEOS_UI_EVIDENCE=1`) refreshes this folder.

```bash
pip install -e ".[dev]"
python -m playwright install chromium
python -m pytest tests/e2e/test_ui_smoke.py -m integration -q
AEOS_UI_EVIDENCE=1 python -m pytest tests/e2e/test_ui_smoke.py -m integration -q
```

The test builds `frontend/dist` when it is missing or older than `frontend/src`, starts its own
server on a free 127.0.0.1 port with a fresh data directory, and never calls Claude, OpenCode,
OpenBao, GitHub, Google or Atlassian: claude/opencode/bao/mmdc point at absent paths, OpenBao at a
closed loopback port, and no GitHub/Google/Atlassian account or credential is configured.
