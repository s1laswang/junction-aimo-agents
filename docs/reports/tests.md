# Session `tests`: keyless pytest (Sun 15 Nov, ~03:00)

**Defect:** on a keyless clone (no `AIMO_API_KEY`, so there is no `data/valio_aimo_junction26_product_data.json`), 8 tests failed. Each one needs the full challenge catalog but was not marked `needs_data`:
- `tests/test_harden.py`: `test_fractional_count_unit_is_422_with_plain_reason`, `test_whole_count_units_still_quote`, `test_fraction_allowed_only_by_a_real_fractional_lot_step`, `test_large_qty_against_standing_baseline_needs_approval`, `test_normal_qty_within_baseline_is_not_flagged`, `test_audit_report_uses_simulated_clock_and_labels_it`, `test_audit_report_real_clock_label`
- `tests/test_need.py::test_split_orders_escalate_once_they_pass_the_auto_limit`

Without the catalog, every one of these hit `FileNotFoundError` (a 500 from the gateway, or the wrong approval tier from the planner).

**Fix:**
- Added `@pytest.mark.needs_data` to all 8 tests. No assertion changed; with the catalog present they run exactly as before.
- `scripts/judge_accept.sh`: a keyless pytest run that exits 0 now reports `PASS pytest (keyless)`. A keyless failure that mentions the catalog file used to be reported as SKIP. It is now FAIL, because it means a test is missing its marker. This stops such failures being hidden.

| run | before | after |
|---|---|---|
| keyed `uv run --extra dev pytest -q` | 344 passed, 0 failed | 344 passed, 7 skipped, 0 failed |
| keyless (git archive, no .env) | 245 passed, **8 failed**, 98 skipped | 245 passed, **0 failed**, 106 skipped |
| keyless `make accept` | pytest SKIP | `PASS pytest (keyless)`, 8/8 scenarios, PASS=17 FAIL=0 SKIP=3, RESULT: PASS |

Human-only approvals were not touched.
