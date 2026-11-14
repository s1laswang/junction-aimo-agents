import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest

from aimo import audit, keys, quotes
from aimo.models import QuoteLine

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ROOT / "deploy"
HASH_CHAR = chr(35)
EM_DASH = "\u2014"


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "aimo.db"
    key_dir = tmp_path / "keys"
    monkeypatch.setenv("AIMO_DB", str(db_path))
    monkeypatch.setenv("AIMO_KEY_DIR", str(key_dir))
    keys._cache.clear()
    pypath = str(ROOT) if not os.environ.get("PYTHONPATH") else f"{ROOT}{os.pathsep}{os.environ['PYTHONPATH']}"
    proc_env = {
        **os.environ,
        "AIMO_DB": str(db_path),
        "AIMO_KEY_DIR": str(key_dir),
        "PYTHONPATH": pypath,
    }
    yield tmp_path, key_dir, proc_env
    keys._cache.clear()


def _run_cli(args, env):
    return subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _run_cli_batch(arg_lists, env):
    with ThreadPoolExecutor(max_workers=len(arg_lists)) as pool:
        return list(pool.map(lambda a: _run_cli(a, env), arg_lists))


def _jws_parts(token):
    header_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    return header_b64, header, payload, sig_b64


def _tamper_payload(token, updates):
    header_b64, _, payload, sig_b64 = _jws_parts(token)
    mutated = {**payload, **updates}
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def test_cli_quote_and_checkpoint_verify_and_key_rotation_walkthrough(isolated_env, capsys):
    tmp_path, key_dir, proc_env = isolated_env
    now_hel = datetime.now(quotes.HELSINKI)
    delivery = now_hel.date() + timedelta(days=2)
    valid_until = (now_hel + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)

    line = QuoteLine(
        product_id="464119",
        name="Valio rasvaton maito 1 l laktoositon UHT",
        quantity=100.0,
        sales_unit="KI",
        unit_price_net=13.32,
        line_total_net=1332.0,
    )
    pre_quote = quotes.issue("hotel-aurora", "buyer-aurora", [line], delivery, valid_until, 1512.0)
    assert pre_quote.quote_id.startswith("q_")

    old_kid = keys.kid()
    active_key_path = key_dir / "quote-ed25519.json"
    assert active_key_path.is_file()
    assert stat.S_IMODE(active_key_path.stat().st_mode) == 0o600

    _, q_header, _, _ = _jws_parts(pre_quote.jws)
    assert q_header == {"alg": "EdDSA", "kid": old_kid}

    audit.record(
        "hotel-aurora",
        "system",
        "order.confirmed",
        "ord_walkthrough",
        f"Mock confirmation, delivery {delivery.isoformat()}.",
        {"order_id": "ord_walkthrough", "quote_id": pre_quote.quote_id, "total_net": 1332.0},
    )
    assert audit.verify() == (True, None)
    pre_cp = audit.checkpoint()
    assert pre_cp["seq"] == 2
    assert re.fullmatch(r"[0-9a-f]{64}", pre_cp["hash"])
    _, cp_header, _, _ = _jws_parts(pre_cp["jws"])
    assert cp_header == {"alg": "EdDSA", "kid": old_kid}

    jwks_file = tmp_path / "jwks.json"
    pre_jwks = keys.jwks()
    assert [k["kid"] for k in pre_jwks["keys"]] == [old_kid]
    assert all("d" not in k and k["alg"] == "EdDSA" for k in pre_jwks["keys"])
    jwks_file.write_text(json.dumps(pre_jwks), encoding="utf-8")

    tampered_quote_jws = _tamper_payload(pre_quote.jws, {"total_gross": 1.0})
    tampered_cp_jws = _tamper_payload(pre_cp["jws"], {"seq": pre_cp["seq"] + 99})

    r_q_ok, r_cp_ok, r_q_bad, r_cp_bad, r_rot = _run_cli_batch(
        [
            ["aimo.quotes", "verify", pre_quote.jws, str(jwks_file)],
            ["aimo.audit", "verify-checkpoint", pre_cp["jws"], str(jwks_file)],
            ["aimo.quotes", "verify", tampered_quote_jws, str(jwks_file)],
            ["aimo.audit", "verify-checkpoint", tampered_cp_jws, str(jwks_file)],
            ["aimo.keys", "rotate"],
        ],
        proc_env,
    )

    expected_pre_quote_out = (
        f"OK {pre_quote.quote_id} customer=hotel-aurora agent=buyer-aurora lines=1 "
        f"total_gross=1512.00 EUR delivery={delivery.isoformat()} valid_until={valid_until.isoformat()}"
    )
    expected_pre_cp_out = f"OK seq={pre_cp['seq']} hash={pre_cp['hash']}"

    assert r_q_ok.returncode == 0, r_q_ok.stderr
    assert r_q_ok.stdout.strip() == expected_pre_quote_out

    assert r_cp_ok.returncode == 0, r_cp_ok.stderr
    assert r_cp_ok.stdout.strip() == expected_pre_cp_out

    assert r_q_bad.returncode == 1
    assert "BAD_SIGNATURE" in r_q_bad.stdout

    assert r_cp_bad.returncode == 1
    assert "BAD_SIGNATURE" in r_cp_bad.stdout

    assert r_rot.returncode == 0, r_rot.stderr
    assert r_rot.stdout.startswith("new kid ")
    new_kid = r_rot.stdout.strip().removeprefix("new kid ").strip()
    assert new_kid and new_kid != old_kid

    retired_file = key_dir / f"retired-{old_kid}.json"
    assert retired_file.is_file()
    retired_jwk = json.loads(retired_file.read_text(encoding="utf-8"))
    assert retired_jwk["kid"] == old_kid
    assert retired_jwk["alg"] == "EdDSA"
    assert "d" not in retired_jwk
    assert stat.S_IMODE(active_key_path.stat().st_mode) == 0o600

    keys._cache.clear()
    assert keys.kid() == new_kid
    post_jwks = keys.jwks()
    assert [k["kid"] for k in post_jwks["keys"]] == [new_kid, old_kid]
    assert all("d" not in k and k["alg"] == "EdDSA" for k in post_jwks["keys"])
    jwks_file.write_text(json.dumps(post_jwks), encoding="utf-8")

    post_quote = quotes.issue("hotel-aurora", "buyer-aurora", [line], delivery, valid_until, 1512.0)
    post_cp = audit.checkpoint()
    assert _jws_parts(post_quote.jws)[1]["kid"] == new_kid
    assert _jws_parts(post_cp["jws"])[1]["kid"] == new_kid
    assert quotes.main(["aimo.quotes", "verify", post_quote.jws, str(jwks_file)]) == 0
    assert audit.main(["aimo.audit", "verify-checkpoint", post_cp["jws"], str(jwks_file)]) == 0
    captured = capsys.readouterr().out
    assert f"OK {post_quote.quote_id} customer=hotel-aurora agent=buyer-aurora " in captured
    assert f"OK seq={post_cp['seq']} hash={post_cp['hash']}" in captured

    expired_quote = quotes.issue(
        "hotel-aurora", "buyer-aurora", [line], delivery, now_hel - timedelta(hours=1), 1512.0
    )
    assert quotes.main(["aimo.quotes", "verify", expired_quote.jws, str(jwks_file)]) == 0
    assert capsys.readouterr().out.strip().endswith(" (EXPIRED)")

    r_pre_q_after, r_pre_cp_after = _run_cli_batch(
        [
            ["aimo.quotes", "verify", pre_quote.jws, str(jwks_file)],
            ["aimo.audit", "verify-checkpoint", pre_cp["jws"], str(jwks_file)],
        ],
        proc_env,
    )

    assert r_pre_q_after.returncode == 0, r_pre_q_after.stderr
    assert r_pre_q_after.stdout.strip() == expected_pre_quote_out

    assert r_pre_cp_after.returncode == 0, r_pre_cp_after.stderr
    assert r_pre_cp_after.stdout.strip() == expected_pre_cp_out


def test_deploy_docs_cli_verification_and_rotation_contract():
    runbook = (DEPLOY_DIR / "RUNBOOK.md").read_text(encoding="utf-8")
    mcp_clients = (DEPLOY_DIR / "mcp-clients.md").read_text(encoding="utf-8")
    public_url = (DEPLOY_DIR / "PUBLIC_URL.md").read_text(encoding="utf-8")

    for snippet in (
        "python -m aimo.quotes verify",
        "python -m aimo.audit verify-checkpoint",
        "python -m aimo.keys rotate",
        "new kid ",
        "retired-<old_kid>.json",
        "quote-ed25519.json",
        "BAD_SIGNATURE",
        "OK q_<id> customer=<customer> agent=<agent_id> lines=<n> total_gross=<amount> EUR delivery=<YYYY-MM-DD> valid_until=<iso>",
        "OK seq=<seq> hash=<sha256_hex>",
    ):
        assert snippet in runbook, f"Missing {snippet!r} in deploy/RUNBOOK.md"

    assert "python -m aimo.quotes verify" in mcp_clients
    assert "python -m aimo.audit verify-checkpoint" in mcp_clients
    assert re.search(
        r"^OK q_[0-9a-f]+ customer=\S+ agent=\S+ lines=\d+ total_gross=\d+\.\d{2} EUR delivery=\d{4}-\d{2}-\d{2} valid_until=\S+$",
        mcp_clients,
        re.MULTILINE,
    )
    assert re.search(r"^OK seq=\d+ hash=[0-9a-f]{64}$", mcp_clients, re.MULTILINE)

    for snippet in (
        "python -m aimo.keys rotate",
        "retired-<kid>.json",
        "quote-ed25519.json",
        "/.well-known/jwks.json",
    ):
        assert snippet in public_url, f"Missing {snippet!r} in deploy/PUBLIC_URL.md"


def test_self_hygiene_no_comments_or_em_dashes():
    src = Path(__file__).read_text(encoding="utf-8")
    assert HASH_CHAR not in src
    assert EM_DASH not in src
