# Peppol Order Response Advanced (T116) export

`aimo/backend/peppol.py` turns a gateway order into a **Peppol BIS Advanced Ordering 3.0 Order Response Advanced** (T116, UBL 2.3):
`CustomizationID urn:fdc:peppol.eu:poacc:trns:order_response_advanced:3`, `ProfileID urn:fdc:peppol.eu:poacc:bis:advanced_ordering:3`
([spec, section 7 and 10.2](https://docs.peppol.eu/poacc/upgrade-3/profiles/65-advanced-ordering/)).

| Gateway | T116 header | Lines |
|---|---|---|
| pending approval / approved, not placed | `AB` received | none |
| `AP` all lines confirmed | `AP` | none (rule R008) |
| `CA` partial | `CA` | all lines: `5` accepted, `3` changed (new `Quantity`), `7` not accepted (rule R007) |
| `RE` | `RE` | none (rule R009), reason in `Note` |

- Samples: [sample-order-response-advanced-AP.xml](sample-order-response-advanced-AP.xml), [sample-order-response-advanced-CA.xml](sample-order-response-advanced-CA.xml), [sample-order-response-advanced-RE.xml](sample-order-response-advanced-RE.xml) (synthetic demo order; regenerate with `uv run python scripts/export_peppol_order_response.py --sample`). Live Gemini run `AP` order artifact, verifiable offline with `scripts/verify_quote.py`: [docs/evidence/live-run/order.json](../evidence/live-run/order.json) ([docs/evidence/live-run/README.md](../evidence/live-run/README.md)).
- Live: `GET /v1/orders/{order_id}/peppol-order-response.xml`.
- **Validation status:** the default test run checks that the samples pass the **PEPPOL-T116-R001 to R010** rules, re-implemented in Python from `rules/sch/parts/PEPPOL-M-T116.sch` in [OpenPEPPOL/poacc-upgrade-3](https://github.com/OpenPEPPOL/poacc-upgrade-3). They also use only element names from the T116 syntax tree. Validation against the OASIS **UBL 2.3 XSD** is an opt-in test (`tests/test_peppol_export.py::test_samples_validate_against_ubl_23_xsd`, run with `AIMO_UBL_XSD=.../UBL-OrderResponse-2.3.xsd` and `uv run --with lxml --extra dev pytest -q tests/test_peppol_export.py`; the schema bundle is not committed), so it is skipped by default. The **full official Peppol schematron was not run** (it needs the generated `T116-basic.sch` and an XSLT2 engine). So this is not yet a certified-valid Peppol document.
- **Placeholders:** the seller and buyer `EndpointID` values are placeholders (`PLACEHOLDER-…`, scheme `0216` OVT). We do not know Valio Aimo's real Peppol IDs. `OrderReference/ID` is our gateway order id, not a buyer ERP order number. The unit codes (`ST→H87`, `KI→CS`, `KG→KGM`, `L→LTR`) are an assumption.
