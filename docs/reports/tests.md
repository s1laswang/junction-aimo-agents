# Session `tests`: keyless pytest (Sun 15 Nov, ~03:00)

Branch / commit: `0f51cf0` on `origin/main`.

**Defect:** on a keyless clone (no `AIMO_API_KEY`, so there is no `data/valio_aimo_junction26_product_data.json`), 8 tests failed. Each one needs the full challenge catalog but was not marked `needs_data`:
- `tests/test_harden.py`: `test_fractional_count_unit_is_422_with_plain_reason`, `test_whole_count_units_still_quote`, `test_fraction_allowed_only_by_a_real_fractional_lot_step`, `test_large_qty_against_standing_baseline_needs_approval`, `test_normal_qty_within_baseline_is_not_flagged`, `test_audit_report_uses_simulated_clock_and_labels_it`, `test_audit_report_real_clock_label`
- `tests/test_need.py::test_split_orders_escalate_once_they_pass_the_auto_limit`

Without the catalog, every one of these hit `FileNotFoundError` (a 500 from the gateway, or the wrong approval tier from the planner).

**Fix:**
- Added `@pytest.mark.needs_data` to all 8 tests. No assertion changed; with the catalog present they run exactly as before.
- `scripts/judge_accept.sh`: a keyless pytest run that exits 0 now reports `PASS pytest (keyless)`. A keyless failure that mentions the catalog file used to be reported as SKIP. It is now FAIL, because it means a test is missing its marker. This stops such failures being hidden.

| run | before | after |
|---|---|---|
| keyed `uv run --extra dev pytest -q` (`0f51cf0`) | 344 passed, 0 failed | 344 passed, 7 skipped, 0 failed |
| keyless (`git archive`, no `.env`, `0f51cf0`) | 245 passed, **8 failed**, 98 skipped | 245 passed, **0 failed**, 106 skipped |
| keyless `make accept` (`0f51cf0` / `9742758`) | pytest SKIP | `PASS pytest (keyless)` (`261 passed, 0 failed` by `9742758`), 8/8 scenarios, PASS=17 FAIL=0 SKIP=3, RESULT: PASS |
| standalone `origin/main` (`02575d8` / `62e8384`) | `371 passed` keyed / `270 passed` keyless (`2fe8975`) | **`375 passed, 7 skipped, 1 xfailed, 1 xpassed`** keyed / **`274 passed, 0 failed`** keyless (`tests/test_live_run_evidence.py`, `tests/test_gemini_fallback.py`, `tests/test_run_api_guard.py`) |
| keyless offline `AIMO_OFFLINE=1 pytest` (`fr8/valio/docs` `c98e72e` -> `1d0b238`) | 0 failed | `1,168 passed, 105 skipped, 1 xfailed, 1 xpassed` across `1,275` collected on `c98e72e`; **`2,448` collected tests** on `1d0b238` (**0 failed**), `scenarios.run_all` **8/8** across **92 checks** |

## Final-freeze alignment (`02575d8` / `62e8384`, `fr8/valio/docs` `1d0b238`)
- **Per-run `judge_accept.sh` temp directory (`02575d8`):** `scripts/judge_accept.sh` on `origin/main` moved log outputs from shared `/tmp/aimo-accept-*.txt` files into a per-run temp directory (`mktemp -d "${TMPDIR:-/tmp}/aimo-accept.XXXXXX"`, `ACCEPT_TMP`; configurable `AIMO_ACCEPT_LOG_PREFIX` + `TMP_ACCEPT` on `fr8/valio`), so parallel `make accept` runs never overwrite each other's pytest or scenario logs.
- **Offline live-run cryptographic verification (`02575d8` / `62e8384`):** `tests/test_live_run_evidence.py` verifies the committed live Gemini hero run in [docs/evidence/live-run/](../evidence/live-run/README.md) (`quote.json` and 6-event `audit-events.json` against `jwks.json` via `scripts/verify_quote.py`) on every keyed and keyless test run (`375 passed` keyed / `274 passed, 0 failed` keyless on `main`).

Human-only approvals were not touched.

## Post-freeze FR8 sync and zero-key browser surfaces
- On `fr8/valio/main` (`43e841c` / `7990fa3`, `1,272` commits) and `fr8/valio/docs` (`ed09d11` / `d89db12`), keyless offline `pytest --collect-only -q tests/` collects **`4,286`** tests (up from `4,137` on `d89db12`, `2,462` on `6343389`, and `2,448` on `1d0b238`), while `scenarios.run_all` passes **8/8** across **92 checks** and `conformance` passes **44/44**.
- Playwright and offline contract tests also cover the **One-Page Judge Walkthrough** ([../demo/index.html](../demo/index.html), `docs/demo/index.html`, WebCrypto Ed25519 JWS + 403 `agent_cannot_approve` + 409 `requote_needed` + Peppol `AP`) and **"Try to Scam the Agent"** ([../arena/](../arena/), `docs/arena/index.html`; `CC0-1.0` Kenney pixel-art ledger in [../demo/ASSETS.md](../demo/ASSETS.md), `docs/demo/ASSETS.md`, and [../arena/assets/manifest.json](../arena/assets/manifest.json)).
