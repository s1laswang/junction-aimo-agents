from __future__ import annotations

import argparse
import os
from datetime import date, datetime

import pytest
import uvicorn
from pydantic import ValidationError

from aimo import cli, gemini
from aimo.buyer import run_demo
from aimo.buyer.need import HELSINKI, MONTHS, parse_need, parse_request, resolve_date
from aimo.gateway import human_auth

SAT = datetime(2026, 11, 14, 15, 0, tzinfo=HELSINKI)


@pytest.mark.parametrize(
    "host,port,expected_url",
    [
        ("0.0.0.0", 8765, "http://127.0.0.1:8765"),
        ("localhost", 8080, "http://127.0.0.1:8080"),
        ("::", 9001, "http://127.0.0.1:9001"),
        ("[::]", 9001, "http://127.0.0.1:9001"),
        ("", 9002, "http://127.0.0.1:9002"),
        ("127.0.0.1", 8000, "http://127.0.0.1:8000"),
        ("::1", 8888, "http://[::1]:8888"),
        ("[::1]", 8888, "http://[::1]:8888"),
        ("2001:db8::42", 8443, "http://[2001:db8::42]:8443"),
        ("192.168.10.25", 8123, "http://192.168.10.25:8123"),
        ("gateway.internal", 7000, "http://gateway.internal:7000"),
    ],
)
def test_export_serve_url_host_and_port_variants(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
    port: int,
    expected_url: str,
) -> None:
    monkeypatch.delenv("AIMO_SERVE_URL", raising=False)
    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    cli._export_serve_url(host, port)
    assert os.environ["AIMO_SERVE_URL"] == expected_url
    assert "AIMO_MCP_URL" not in os.environ
    assert capsys.readouterr().out == ""


def test_export_serve_url_rewrites_stale_local_mcp_url_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AIMO_MCP_URL", "http://localhost:8000/mcp")
    cli._export_serve_url("0.0.0.0", 8765)
    out = capsys.readouterr().out
    assert os.environ["AIMO_SERVE_URL"] == "http://127.0.0.1:8765"
    assert os.environ["AIMO_MCP_URL"] == "http://127.0.0.1:8765/mcp"
    assert "warning:" in out
    assert "does not match the served port 8765" in out

    monkeypatch.setenv("AIMO_MCP_URL", "http://127.0.0.1:8000")
    cli._export_serve_url("localhost", 8900)
    out_v4 = capsys.readouterr().out
    assert os.environ["AIMO_MCP_URL"] == "http://127.0.0.1:8900/mcp"
    assert "warning:" in out_v4

    monkeypatch.setenv("AIMO_MCP_URL", "http://[::1]:8000/mcp")
    cli._export_serve_url("::1", 8901)
    out_v6 = capsys.readouterr().out
    assert os.environ["AIMO_SERVE_URL"] == "http://[::1]:8901"
    assert os.environ["AIMO_MCP_URL"] == "http://[::1]:8901/mcp"
    assert "warning:" in out_v6


def test_export_serve_url_preserves_matching_local_and_external_mcp_urls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AIMO_MCP_URL", "http://127.0.0.1:8765/mcp")
    cli._export_serve_url("127.0.0.1", 8765)
    assert os.environ["AIMO_MCP_URL"] == "http://127.0.0.1:8765/mcp"
    assert capsys.readouterr().out == ""

    monkeypatch.setenv("AIMO_MCP_URL", "https://gateway.example.org/mcp")
    cli._export_serve_url("0.0.0.0", 8765)
    assert os.environ["AIMO_SERVE_URL"] == "http://127.0.0.1:8765"
    assert os.environ["AIMO_MCP_URL"] == "https://gateway.example.org/mcp"
    assert capsys.readouterr().out == ""

    monkeypatch.setenv("AIMO_MCP_URL", "http://10.20.30.40:8000/mcp")
    cli._export_serve_url("0.0.0.0", 8765)
    assert os.environ["AIMO_MCP_URL"] == "http://10.20.30.40:8000/mcp"
    assert capsys.readouterr().out == ""


def test_cmd_serve_exports_serve_url_before_credential_warning_and_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIMO_SERVE_URL", raising=False)
    monkeypatch.delenv("AIMO_MCP_URL", raising=False)
    events: list[tuple[str, str | None]] = []

    monkeypatch.setattr(cli, "_fetch", lambda force=False: events.append(("fetch", os.environ.get("AIMO_SERVE_URL"))))
    monkeypatch.setattr(cli, "_build_catalog", lambda mode: events.append(("build", os.environ.get("AIMO_SERVE_URL"))))
    monkeypatch.setattr(
        human_auth,
        "credential_warning",
        lambda: events.append(("cred", os.environ.get("AIMO_SERVE_URL"))) or None,
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda *args, **kwargs: events.append(("uvicorn", os.environ.get("AIMO_SERVE_URL"))),
    )

    ns = argparse.Namespace(host="0.0.0.0", port=8793, reload=False, no_build=True, build_sync=False)
    cli.cmd_serve(ns)

    assert events == [
        ("fetch", None),
        ("build", None),
        ("cred", "http://127.0.0.1:8793"),
        ("uvicorn", "http://127.0.0.1:8793"),
    ]


@pytest.mark.parametrize(
    "demo_rc,expected_exit",
    [
        (1, 1),
        (2, 2),
        (42, 42),
        (0, 0),
        (None, 0),
    ],
)
def test_cmd_demo_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    demo_rc: int | None,
    expected_exit: int,
) -> None:
    received: list[list[str]] = []

    def fake_demo_main(argv: list[str] | None = None) -> int | None:
        received.append(list(argv or []))
        return demo_rc

    monkeypatch.setattr(run_demo, "main", fake_demo_main)
    with pytest.raises(SystemExit) as ei:
        cli.main(["demo", "--transport", "fake", "--no-gemini"])
    assert ei.value.code == expected_exit
    assert received == [["--transport", "fake", "--no-gemini"]]


@pytest.mark.parametrize(
    "nom,part,month_num,expected_date",
    [
        ("tammikuu", "tammikuuta", 1, date(2027, 1, 10)),
        ("helmikuu", "helmikuuta", 2, date(2027, 2, 10)),
        ("maaliskuu", "maaliskuuta", 3, date(2027, 3, 10)),
        ("huhtikuu", "huhtikuuta", 4, date(2027, 4, 10)),
        ("toukokuu", "toukokuuta", 5, date(2027, 5, 10)),
        ("kesäkuu", "kesäkuuta", 6, date(2027, 6, 10)),
        ("heinäkuu", "heinäkuuta", 7, date(2027, 7, 10)),
        ("elokuu", "elokuuta", 8, date(2027, 8, 10)),
        ("syyskuu", "syyskuuta", 9, date(2027, 9, 10)),
        ("lokakuu", "lokakuuta", 10, date(2027, 10, 10)),
        ("marraskuu", "marraskuuta", 11, date(2027, 11, 10)),
        ("joulukuu", "joulukuuta", 12, date(2026, 12, 10)),
    ],
)
def test_finnish_months_nominative_and_partitive_in_months_and_resolve_date(
    nom: str,
    part: str,
    month_num: int,
    expected_date: date,
) -> None:
    assert MONTHS[nom] == month_num
    assert MONTHS[part] == month_num
    res_part = resolve_date(f"10. {part}", SAT)
    res_nom = resolve_date(f"10 {nom}", SAT)
    assert res_part is not None and res_part[0] == expected_date
    assert res_nom is not None and res_nom[0] == expected_date


@pytest.mark.parametrize(
    "phrase,expected_date",
    [
        ("16. marraskuuta", date(2026, 11, 16)),
        ("18. marraskuuta mennessä", date(2026, 11, 18)),
        ("1. joulukuuta", date(2026, 12, 1)),
        ("marraskuun 18.", date(2026, 11, 18)),
        ("18.11.", date(2026, 11, 18)),
        ("16:e november", date(2026, 11, 16)),
        ("21:a november", date(2026, 11, 21)),
        ("16. november", date(2026, 11, 16)),
        ("15 januari", date(2027, 1, 15)),
        ("16th November", date(2026, 11, 16)),
        ("21st of November", date(2026, 11, 21)),
        ("November 18th", date(2026, 11, 18)),
        ("16. November", date(2026, 11, 16)),
    ],
)
def test_fi_sv_en_month_and_dotted_resolve_date(phrase: str, expected_date: date) -> None:
    got = resolve_date(phrase, SAT)
    assert got is not None
    assert got[0] == expected_date


@pytest.mark.parametrize(
    "text,customer,expected_query,expected_qty,expected_unit,expected_date",
    [
        (
            "Tarvitsemme 200 litraa kaurajuomaa 18. marraskuuta mennessä.",
            "school-tampere",
            "oat drink",
            200.0,
            "L",
            date(2026, 11, 18),
        ),
        (
            "Tilaa 100 litraa laktoositonta maitoa 16. marraskuuta.",
            "hotel-aurora",
            "lactose-free milk",
            100.0,
            "L",
            date(2026, 11, 16),
        ),
        (
            "18. marraskuuta mennessä tarvitsemme 200 litraa kaurajuomaa.",
            "school-tampere",
            "oat drink",
            200.0,
            "L",
            date(2026, 11, 18),
        ),
        (
            "Tarvitsemme 30 kg voita marraskuun 18. mennessä.",
            "hotel-aurora",
            "butter",
            30.0,
            "KG",
            date(2026, 11, 18),
        ),
        (
            "Tarvitsemme 2,5 kg juustoraastetta 18.11. mennessä.",
            "hotel-aurora",
            "grated cheese",
            2.5,
            "KG",
            date(2026, 11, 18),
        ),
        (
            "Vi behöver 120 liter havredryck till 16:e november.",
            "school-tampere",
            "oat drink",
            120.0,
            "L",
            date(2026, 11, 16),
        ),
        (
            "Vi behöver 120 liter havredryck senast den 21:a november.",
            "school-tampere",
            "oat drink",
            120.0,
            "L",
            date(2026, 11, 21),
        ),
        (
            "Vi behöver 25 kg smör till 15. december.",
            "hotel-aurora",
            "butter",
            25.0,
            "KG",
            date(2026, 12, 15),
        ),
        (
            "We need 150 litres of lactose-free milk by 16. November.",
            "hotel-aurora",
            "lactose-free milk",
            150.0,
            "L",
            date(2026, 11, 16),
        ),
        (
            "We need 40 L whipping cream by November 19th.",
            "hotel-aurora",
            "whipping cream",
            40.0,
            "L",
            date(2026, 11, 19),
        ),
    ],
)
def test_parse_request_keyless_strips_month_dates_cleanly(
    text: str,
    customer: str,
    expected_query: str,
    expected_qty: float,
    expected_unit: str,
    expected_date: date,
) -> None:
    needs = parse_request(text, customer, SAT, use_gemini=False)
    assert len(needs) == 1
    need = needs[0]
    assert need.product_query == expected_query
    assert need.quantity == pytest.approx(expected_qty)
    assert need.unit == expected_unit
    assert need.delivery_by == expected_date
    assert "delivery_date_assumed" not in need.preferences


def test_parse_request_keyless_multi_item_with_finnish_month_date() -> None:
    needs = parse_request(
        "Tarvitsemme 40 litraa ruokakermaa ja 20 kiloa voita 18. marraskuuta mennessä, laktoositonta.",
        "hotel-aurora",
        SAT,
        use_gemini=False,
    )
    assert [(n.product_query, n.quantity, n.unit) for n in needs] == [
        ("cooking cream", 40.0, "L"),
        ("butter", 20.0, "KG"),
    ]
    assert all(n.delivery_by == date(2026, 11, 18) for n in needs)
    assert all(n.required_claims == ["FREE_FROM_LACTOSE"] for n in needs)
    assert all("delivery_date_assumed" not in n.preferences for n in needs)


@pytest.mark.parametrize(
    "bad_text",
    [
        "We need 0 litres of lactose-free milk by Monday.",
        "We need -10 kg butter by 18. November.",
        "Tilaa 0 litraa maitoa 16. marraskuuta mennessä.",
        "Tarvitsemme -5 kiloa voita 18. marraskuuta mennessä.",
        "Vi behöver 0 liter havredryck till 16:e november.",
        "Vi behöver -3 kg smör till måndag.",
        "We need 20 L cream and 0 kg butter by Monday.",
    ],
)
def test_parse_request_use_gemini_true_rejects_non_positive_qty_before_gemini(
    monkeypatch: pytest.MonkeyPatch,
    bad_text: str,
) -> None:
    calls: list[tuple[object, ...]] = []

    def fail_if_called(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("aimo.gemini.generate must not be called for non-positive quantity")

    monkeypatch.setattr(gemini, "generate", fail_if_called)

    with pytest.raises(ValidationError):
        parse_request(bad_text, "hotel-aurora", SAT, use_gemini=True)
    assert calls == []

    with pytest.raises(ValidationError):
        parse_need(bad_text, "hotel-aurora", SAT, use_gemini=True)
    assert calls == []


@pytest.mark.parametrize("bad_qty", [0.0, -2.5])
def test_parse_request_use_gemini_true_rejects_non_positive_model_qty_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    bad_qty: float,
) -> None:
    class _FakeResp:
        text = (
            '{"items": [{"product_query": "lactose-free milk", "required_claims": [], '
            f'"excluded_allergens": [], "quantity": {bad_qty}, "unit": "L", '
            '"delivery_by": "2026-11-16", "delivery_expression": "by Monday", "site": "", "notes": ""}]}'
        )

    monkeypatch.setattr(gemini, "generate", lambda *args, **kwargs: _FakeResp())
    with pytest.raises(ValidationError):
        parse_request("zero litres of lactose-free milk by Monday", "hotel-aurora", SAT, use_gemini=True)

