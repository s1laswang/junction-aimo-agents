import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from joserfc import jws
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, OKPKey
import pytest

from aimo import audit, discovery, keys, quotes
from aimo.models import Quote, QuoteLine

pytestmark = pytest.mark.filterwarnings("ignore:EdDSA is deprecated")

B64URL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
EXPECTED_PUBLIC_JWK_FIELDS = {"kty", "crv", "alg", "use", "kid", "x"}


@pytest.fixture(autouse=True)
def isolated_crypto_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMO_DB", str(tmp_path / "crypto.db"))
    monkeypatch.setenv("AIMO_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("AIMO_DATA_DIR", str(tmp_path / "nonexistent_data"))
    monkeypatch.delenv("VALIO_API_BASE", raising=False)
    monkeypatch.delenv("VALIO_API_KEY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    keys._cache.clear()
    try:
        yield tmp_path
    finally:
        keys._cache.clear()


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(seg: str) -> bytes:
    pad = "=" * ((4 - len(seg) % 4) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _b64u_json(obj: dict) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _b64u_encode(raw)


def _split_jws(token: str) -> tuple[dict, bytes, bytes, str, str, str]:
    parts = token.split(".")
    assert len(parts) == 3
    h_b64, p_b64, s_b64 = parts
    header = json.loads(_b64u_decode(h_b64).decode("utf-8"))
    payload_bytes = _b64u_decode(p_b64)
    sig_bytes = _b64u_decode(s_b64)
    return header, payload_bytes, sig_bytes, h_b64, p_b64, s_b64


def _flip_sig_byte(s_b64: str, index: int) -> str:
    buf = bytearray(_b64u_decode(s_b64))
    buf[index] ^= 0xFF
    return _b64u_encode(bytes(buf))


def _make_foreign_jwks() -> tuple[OKPKey, dict]:
    foreign_key = OKPKey.generate_key("Ed25519")
    foreign_kid = foreign_key.thumbprint()
    foreign_pub = {
        **foreign_key.as_dict(private=False),
        "kid": foreign_kid,
        "alg": "EdDSA",
        "use": "sig",
    }
    return foreign_key, {"keys": [foreign_pub]}


def _issue_quote(
    customer: str = "hotel-aurora",
    agent_id: str = "agent-alpha",
    hours: float = 2.0,
    extra_lines: list[QuoteLine] | None = None,
) -> Quote:
    lines = extra_lines or [
        QuoteLine(
            product_id="400101",
            name="Valio Laktoositon Kevytmaito 1 L",
            quantity=10.0,
            sales_unit="LTR",
            unit_price_net=1.25,
            line_total_net=12.50,
        )
    ]
    delivery = datetime.now(quotes.HELSINKI).date() + timedelta(days=2)
    valid_until = datetime.now(timezone.utc) + timedelta(hours=hours)
    net = round(sum(line.line_total_net for line in lines), 2)
    gross = round(net * 1.14, 2)
    return quotes.issue(customer, agent_id, lines, delivery, valid_until, gross)


@contextmanager
def _serve_jwks():
    _, foreign_jwks = _make_foreign_jwks()
    requests_seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(self.path)
            if self.path == "/.well-known/jwks.json":
                body = json.dumps(keys.jwks()).encode("utf-8")
            elif self.path == "/foreign-jwks.json":
                body = json.dumps(foreign_jwks).encode("utf-8")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    try:
        yield base_url, requests_seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _assert_public_jwk_hygiene(jwk_dict: dict) -> None:
    assert set(jwk_dict.keys()) == EXPECTED_PUBLIC_JWK_FIELDS
    assert "d" not in jwk_dict
    assert jwk_dict["kty"] == "OKP"
    assert jwk_dict["crv"] == "Ed25519"
    assert jwk_dict["alg"] == "EdDSA"
    assert jwk_dict["use"] == "sig"
    assert isinstance(jwk_dict["kid"], str) and len(jwk_dict["kid"]) >= 32
    raw_x = _b64u_decode(jwk_dict["x"])
    assert len(raw_x) == 32
    thumb_input = json.dumps(
        {"crv": jwk_dict["crv"], "kty": jwk_dict["kty"], "x": jwk_dict["x"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_thumbprint = _b64u_encode(hashlib.sha256(thumb_input).digest())
    assert jwk_dict["kid"] == expected_thumbprint


def test_cold_start_autogenerates_ed25519_keypair_and_exposes_public_only(tmp_path):
    key_dir = Path(os.environ["AIMO_KEY_DIR"])
    assert not key_dir.exists()

    first_kid = keys.kid()
    key_file = key_dir / "quote-ed25519.json"
    assert key_file.is_file()
    assert not (key_dir / "quote-ed25519.tmp").exists()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    disk_jwk = json.loads(key_file.read_text(encoding="utf-8"))
    assert disk_jwk["kty"] == "OKP"
    assert disk_jwk["crv"] == "Ed25519"
    assert "d" in disk_jwk
    assert len(_b64u_decode(disk_jwk["d"])) == 32

    pub = keys.public_jwk()
    _assert_public_jwk_hygiene(pub)
    assert pub["kid"] == first_kid

    jwks_doc = keys.jwks()
    assert list(jwks_doc.keys()) == ["keys"]
    assert len(jwks_doc["keys"]) == 1
    _assert_public_jwk_hygiene(jwks_doc["keys"][0])
    assert jwks_doc["keys"][0] == pub


def test_cold_start_key_persists_across_cache_clear():
    k0 = keys.kid()
    pub0 = keys.public_jwk()
    jwks0 = keys.jwks()
    q_before = _issue_quote(agent_id="agent-before")
    audit.record("hotel-aurora", "system", "order.placed", "o_persist", "Persisted key check.")
    cp_before = audit.checkpoint()

    keys._cache.clear()
    assert keys._cache == {}

    assert keys.kid() == k0
    assert keys.public_jwk() == pub0
    assert keys.jwks() == jwks0

    verified_q = quotes.verify(q_before.jws)
    assert verified_q.quote_id == q_before.quote_id
    redeemed_q = quotes.redeem(q_before.quote_id, q_before.jws, "hotel-aurora", "agent-before")
    assert redeemed_q.quote_id == q_before.quote_id
    assert audit.verify_checkpoint(cp_before["jws"]) == {
        "seq": cp_before["seq"],
        "hash": cp_before["hash"],
        "ts": cp_before["ts"],
    }

    q_after = _issue_quote(agent_id="agent-after")
    h_after, _, _, _, _, _ = _split_jws(q_after.jws)
    assert h_after["kid"] == k0


def test_three_rotations_preserve_all_generations_and_verify_quotes_and_checkpoints():
    generations = []

    for idx in range(4):
        if idx > 0:
            new_kid = keys.rotate()
            assert new_kid == keys.kid()
        active_kid = keys.kid()
        q_for_verify = _issue_quote(agent_id=f"verify-gen-{idx}")
        q_for_redeem = _issue_quote(agent_id=f"redeem-gen-{idx}")
        ev = audit.record(
            "hotel-aurora",
            f"agent:gen-{idx}",
            "order.placed",
            f"ord_gen_{idx}",
            f"Recorded under key generation {idx}.",
            {"generation": idx},
        )
        cp = audit.checkpoint()
        assert cp["hash"] == ev.hash
        generations.append((active_kid, q_for_verify, q_for_redeem, cp))

        if idx == 1:
            assert [entry["kid"] for entry in keys.jwks()["keys"]] == [
                generations[1][0],
                generations[0][0],
            ]

    k0, k1, k2, k3 = [g[0] for g in generations]
    assert len({k0, k1, k2, k3}) == 4

    latest_jwks = keys.jwks()
    jwks_kids = [entry["kid"] for entry in latest_jwks["keys"]]
    assert len(jwks_kids) == 4
    assert jwks_kids[0] == k3
    assert jwks_kids[1:] == sorted([k0, k1, k2])
    assert set(jwks_kids) == {k0, k1, k2, k3}

    for entry in latest_jwks["keys"]:
        _assert_public_jwk_hygiene(entry)

    key_dir = Path(os.environ["AIMO_KEY_DIR"])
    retired_files = sorted(key_dir.glob("retired-*.json"))
    assert len(retired_files) == 3
    assert not (key_dir / "quote-ed25519.tmp").exists()
    for rf in retired_files:
        retired_jwk = json.loads(rf.read_text(encoding="utf-8"))
        _assert_public_jwk_hygiene(retired_jwk)

    keys._cache.clear()
    assert keys.kid() == k3
    assert keys.jwks() == latest_jwks
    assert discovery.jwks() == latest_jwks

    for gen_kid, q_ver, q_red, cp in generations:
        q_header, _, _, _, _, _ = _split_jws(q_ver.jws)
        cp_header, _, _, _, _, _ = _split_jws(cp["jws"])
        assert q_header == {"alg": "EdDSA", "kid": gen_kid}
        assert cp_header == {"alg": "EdDSA", "kid": gen_kid}

        assert quotes.verify(q_ver.jws).quote_id == q_ver.quote_id
        assert quotes.verify(q_ver.jws, latest_jwks).quote_id == q_ver.quote_id

        expected_cp = {"seq": cp["seq"], "hash": cp["hash"], "ts": cp["ts"]}
        assert audit.verify_checkpoint(cp["jws"]) == expected_cp
        assert audit.verify_checkpoint(cp["jws"], latest_jwks) == expected_cp

        redeemed = quotes.redeem(q_red.quote_id, q_red.jws, q_red.customer, q_red.agent_id)
        assert redeemed.quote_id == q_red.quote_id
        with pytest.raises(quotes.QuoteError) as exc_info:
            quotes.redeem(q_red.quote_id, q_red.jws, q_red.customer, q_red.agent_id)
        assert exc_info.value.code == "QUOTE_USED"

    assert audit.verify() == (True, None)


def test_single_generation_subset_jwks_isolates_matching_kid_only():
    k0 = keys.kid()
    pub_k0 = keys.public_jwk()
    q0 = _issue_quote(agent_id="gen0")
    cp0 = audit.checkpoint()

    k1 = keys.rotate()
    assert k1 != k0
    q1 = _issue_quote(agent_id="gen1")
    cp1 = audit.checkpoint()

    only_k0_jwks = {"keys": [pub_k0]}
    assert quotes.verify(q0.jws, only_k0_jwks).quote_id == q0.quote_id
    assert audit.verify_checkpoint(cp0["jws"], only_k0_jwks)["seq"] == cp0["seq"]

    with pytest.raises(quotes.QuoteError) as q_err:
        quotes.verify(q1.jws, only_k0_jwks)
    assert q_err.value.code == "BAD_SIGNATURE"

    with pytest.raises(ValueError, match="BAD_SIGNATURE"):
        audit.verify_checkpoint(cp1["jws"], only_k0_jwks)


def test_quote_jws_rfc8037_structure_canonical_json_and_independent_crypto():
    lines = [
        QuoteLine(
            product_id="400101",
            name="Valio Täysmaito Laktoositon 1 L, Pitäjänmäki",
            quantity=6.0,
            sales_unit="LTR",
            unit_price_net=1.40,
            line_total_net=8.40,
        ),
        QuoteLine(
            product_id="400202",
            name="Valio Kaurajuoma Barista 1 L, Äänekoski",
            quantity=4.0,
            sales_unit="LTR",
            unit_price_net=2.10,
            line_total_net=8.40,
            substitute_for="400199",
        ),
    ]
    q = _issue_quote(customer="hotel-aurora", agent_id="buyer-fi", extra_lines=lines)
    active_kid = keys.kid()
    pub = keys.public_jwk()
    jwks_doc = keys.jwks()

    header, payload_bytes, sig_bytes, h_b64, p_b64, s_b64 = _split_jws(q.jws)
    for seg in (h_b64, p_b64, s_b64):
        assert "=" not in seg
        assert B64URL_SEGMENT_RE.match(seg)

    assert header == {"alg": "EdDSA", "kid": active_kid}
    raw_header_str = _b64u_decode(h_b64).decode("utf-8")
    assert raw_header_str == json.dumps({"alg": "EdDSA", "kid": active_kid}, sort_keys=True, separators=(",", ":"))

    raw_payload_str = payload_bytes.decode("utf-8")
    payload_dict = json.loads(raw_payload_str)
    assert "jws" not in payload_dict
    assert list(payload_dict.keys()) == sorted(payload_dict.keys())
    for line_dict in payload_dict["lines"]:
        assert list(line_dict.keys()) == sorted(line_dict.keys())

    expected_canon = json.dumps(
        q.model_dump(mode="json", exclude={"jws"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert raw_payload_str == expected_canon
    assert raw_payload_str == json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))

    with sqlite3.connect(os.environ["AIMO_DB"]) as con:
        stored_payload, stored_jws = con.execute(
            "SELECT payload, jws FROM quotes WHERE quote_id=?",
            (q.quote_id,),
        ).fetchone()
    assert stored_payload == raw_payload_str
    assert stored_jws == q.jws

    assert len(sig_bytes) == 64
    raw_pub = _b64u_decode(pub["x"])
    assert len(raw_pub) == 32
    Ed25519PublicKey.from_public_bytes(raw_pub).verify(
        sig_bytes,
        f"{h_b64}.{p_b64}".encode("ascii"),
    )

    ks = KeySet.import_key_set(jwks_doc)
    verified_obj = jws.deserialize_compact(q.jws, ks, algorithms=["EdDSA"])
    assert verified_obj.headers() == {"alg": "EdDSA", "kid": active_kid}
    reconstructed = Quote(**json.loads(verified_obj.payload), jws=q.jws)
    assert reconstructed == q


def test_checkpoint_jws_rfc8037_genesis_and_populated_canonical_invariants():
    genesis_cp = audit.checkpoint()
    assert genesis_cp["seq"] == 0
    assert genesis_cp["hash"] == "0" * 64
    assert genesis_cp["ts"] is None

    g_header, g_payload_bytes, g_sig, g_h, g_p, g_s = _split_jws(genesis_cp["jws"])
    for seg in (g_h, g_p, g_s):
        assert "=" not in seg and B64URL_SEGMENT_RE.match(seg)
    assert g_header == {"alg": "EdDSA", "kid": keys.kid()}
    g_raw = g_payload_bytes.decode("utf-8")
    g_dict = json.loads(g_raw)
    assert "jws" not in g_dict
    assert list(g_dict.keys()) == ["hash", "seq", "ts"]
    assert g_raw == json.dumps(
        {"hash": "0" * 64, "seq": 0, "ts": None},
        sort_keys=True,
        separators=(",", ":"),
    )

    ks = KeySet.import_key_set(keys.jwks())
    g_obj = jws.deserialize_compact(genesis_cp["jws"], ks, algorithms=["EdDSA"])
    assert json.loads(g_obj.payload) == {"seq": 0, "hash": "0" * 64, "ts": None}
    Ed25519PublicKey.from_public_bytes(_b64u_decode(keys.public_jwk()["x"])).verify(
        g_sig,
        f"{g_h}.{g_p}".encode("ascii"),
    )

    audit.record("hotel-aurora", "agent:buyer", "parse.request", "need_1", "Pyydettiin maitoa.", {"items": 2.0})
    last_ev = audit.record(
        "hotel-aurora",
        "system",
        "order.confirmed",
        "ord_99",
        "Vahvistettu toimitus Jyväskylään.",
        {"order_id": "ord_99", "total_gross": 185.5},
    )
    pop_cp = audit.checkpoint()
    assert pop_cp["seq"] == 2
    assert pop_cp["hash"] == last_ev.hash
    assert isinstance(pop_cp["ts"], str) and pop_cp["ts"].endswith("Z")

    p_header, p_payload_bytes, p_sig, p_h, p_p, _ = _split_jws(pop_cp["jws"])
    assert p_header == {"alg": "EdDSA", "kid": keys.kid()}
    p_raw = p_payload_bytes.decode("utf-8")
    p_dict = json.loads(p_raw)
    assert "jws" not in p_dict
    assert list(p_dict.keys()) == ["hash", "seq", "ts"]
    assert p_raw == json.dumps(
        {"seq": pop_cp["seq"], "hash": pop_cp["hash"], "ts": pop_cp["ts"]},
        sort_keys=True,
        separators=(",", ":"),
    )

    p_obj = jws.deserialize_compact(pop_cp["jws"], ks, algorithms=["EdDSA"])
    assert p_obj.headers() == {"alg": "EdDSA", "kid": keys.kid()}
    assert json.loads(p_obj.payload) == {
        "seq": pop_cp["seq"],
        "hash": pop_cp["hash"],
        "ts": pop_cp["ts"],
    }
    Ed25519PublicKey.from_public_bytes(_b64u_decode(keys.public_jwk()["x"])).verify(
        p_sig,
        f"{p_h}.{p_p}".encode("ascii"),
    )


def test_cli_http_url_file_and_default_jwks_verification_across_rotations(tmp_path, capsys):
    issued_quotes = []
    issued_checkpoints = []

    for gen in range(4):
        if gen > 0:
            keys.rotate()
        q = _issue_quote(agent_id=f"cli-agent-{gen}")
        cp = audit.checkpoint()
        issued_quotes.append(q)
        issued_checkpoints.append(cp)

    expired_quote = _issue_quote(agent_id="cli-expired", hours=-1.0)

    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps(keys.jwks()), encoding="utf-8")

    with _serve_jwks() as (base_url, requests_seen):
        jwks_url = f"{base_url}/.well-known/jwks.json"
        assert quotes._load_jwks(jwks_url) == keys.jwks()
        assert quotes._load_jwks(str(jwks_file)) == keys.jwks()

        for q, cp in zip(issued_quotes, issued_checkpoints):
            for extra_arg in ([jwks_url], [str(jwks_file)], []):
                rc_q = quotes.main(["quotes.py", "verify", q.jws, *extra_arg])
                out_q = capsys.readouterr().out
                assert rc_q == 0
                assert f"OK {q.quote_id}" in out_q
                assert f"customer={q.customer}" in out_q
                assert "(EXPIRED)" not in out_q

                rc_cp = audit.main(["audit.py", "verify-checkpoint", cp["jws"], *extra_arg])
                out_cp = capsys.readouterr().out
                assert rc_cp == 0
                assert f"OK seq={cp['seq']} hash={cp['hash']}" in out_cp

        rc_exp = quotes.main(["quotes.py", "verify", expired_quote.jws, jwks_url])
        out_exp = capsys.readouterr().out
        assert rc_exp == 0
        assert f"OK {expired_quote.quote_id}" in out_exp
        assert "(EXPIRED)" in out_exp

        assert requests_seen.count("/.well-known/jwks.json") >= 10


def test_cli_subprocess_entrypoints_over_loopback_http():
    q = _issue_quote(agent_id="subproc-agent")
    cp = audit.checkpoint()
    _, _, _, h_b64, p_b64, s_b64 = _split_jws(q.jws)
    tampered_q = f"{h_b64}.{p_b64}.{_flip_sig_byte(s_b64, 0)}"
    _, _, _, ch_b64, cp_b64, cs_b64 = _split_jws(cp["jws"])
    tampered_cp = f"{ch_b64}.{cp_b64}.{_flip_sig_byte(cs_b64, 0)}"

    root = str(Path(__file__).resolve().parents[1])
    existing_pypath = os.environ.get("PYTHONPATH", "")
    pypath = root if not existing_pypath else f"{root}{os.pathsep}{existing_pypath}"
    env = {**os.environ, "PYTHONPATH": pypath}

    with _serve_jwks() as (base_url, requests_seen):
        jwks_url = f"{base_url}/.well-known/jwks.json"

        ok_q = subprocess.run(
            [sys.executable, "-m", "aimo.quotes", "verify", q.jws, jwks_url],
            capture_output=True,
            text=True,
            env=env,
        )
        assert ok_q.returncode == 0
        assert f"OK {q.quote_id}" in ok_q.stdout

        ok_cp = subprocess.run(
            [sys.executable, "-m", "aimo.audit", "verify-checkpoint", cp["jws"], jwks_url],
            capture_output=True,
            text=True,
            env=env,
        )
        assert ok_cp.returncode == 0
        assert f"OK seq={cp['seq']} hash={cp['hash']}" in ok_cp.stdout

        bad_q = subprocess.run(
            [sys.executable, "-m", "aimo.quotes", "verify", tampered_q, jwks_url],
            capture_output=True,
            text=True,
            env=env,
        )
        assert bad_q.returncode == 1
        assert "BAD_SIGNATURE" in bad_q.stdout

        bad_cp = subprocess.run(
            [sys.executable, "-m", "aimo.audit", "verify-checkpoint", tampered_cp, jwks_url],
            capture_output=True,
            text=True,
            env=env,
        )
        assert bad_cp.returncode == 1
        assert "BAD_SIGNATURE" in bad_cp.stdout

        usage_q = subprocess.run(
            [sys.executable, "-m", "aimo.quotes"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert usage_q.returncode == 2

        usage_cp = subprocess.run(
            [sys.executable, "-m", "aimo.audit"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert usage_cp.returncode == 2
        assert len(requests_seen) >= 4


def test_cli_rejects_tampered_tokens_and_wrong_jwks_with_exit_code_1(tmp_path, capsys):
    q = _issue_quote(agent_id="tamper-target")
    cp = audit.checkpoint()

    q_header, q_payload_bytes, _, q_h, q_p, q_s = _split_jws(q.jws)
    cp_header, cp_payload_bytes, _, cp_h, cp_p, cp_s = _split_jws(cp["jws"])

    q_edited = json.loads(q_payload_bytes.decode("utf-8"))
    q_edited["total_gross"] = 0.01
    cp_edited = json.loads(cp_payload_bytes.decode("utf-8"))
    cp_edited["seq"] = cp_edited["seq"] + 99

    foreign_key, foreign_jwks = _make_foreign_jwks()
    foreign_jwks_file = tmp_path / "foreign-jwks.json"
    foreign_jwks_file.write_text(json.dumps(foreign_jwks), encoding="utf-8")

    forged_q_foreign_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": foreign_key.thumbprint()},
        q_payload_bytes,
        foreign_key,
        algorithms=["EdDSA"],
    )
    forged_q_spoofed_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": q_header["kid"]},
        q_payload_bytes,
        foreign_key,
        algorithms=["EdDSA"],
    )
    forged_cp_foreign_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": foreign_key.thumbprint()},
        cp_payload_bytes,
        foreign_key,
        algorithms=["EdDSA"],
    )
    forged_cp_spoofed_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": cp_header["kid"]},
        cp_payload_bytes,
        foreign_key,
        algorithms=["EdDSA"],
    )

    bad_quote_tokens = [
        f"{q_h}.{q_p}.{_flip_sig_byte(q_s, 0)}",
        f"{q_h}.{q_p}.{_flip_sig_byte(q_s, 31)}",
        f"{q_h}.{q_p}.{_flip_sig_byte(q_s, 63)}",
        f"{q_h}.{_b64u_json(q_edited)}.{q_s}",
        f"{q_h}.{q_p}.",
        "not-a-compact-jws",
        "a.b.c",
        forged_q_foreign_kid,
        forged_q_spoofed_kid,
    ]
    bad_cp_tokens = [
        f"{cp_h}.{cp_p}.{_flip_sig_byte(cp_s, 0)}",
        f"{cp_h}.{cp_p}.{_flip_sig_byte(cp_s, 31)}",
        f"{cp_h}.{cp_p}.{_flip_sig_byte(cp_s, 63)}",
        f"{cp_h}.{_b64u_json(cp_edited)}.{cp_s}",
        f"{cp_h}.{cp_p}.",
        "not-a-compact-jws",
        "a.b.c",
        forged_cp_foreign_kid,
        forged_cp_spoofed_kid,
    ]

    real_jwks_file = tmp_path / "real-jwks.json"
    real_jwks_file.write_text(json.dumps(keys.jwks()), encoding="utf-8")

    with _serve_jwks() as (base_url, _):
        real_url = f"{base_url}/.well-known/jwks.json"
        foreign_url = f"{base_url}/foreign-jwks.json"

        for tok in bad_quote_tokens:
            assert quotes.main(["quotes.py", "verify", tok]) == 1
            assert quotes.main(["quotes.py", "verify", tok, str(real_jwks_file)]) == 1
            assert quotes.main(["quotes.py", "verify", tok, real_url]) == 1

        for tok in bad_cp_tokens:
            assert audit.main(["audit.py", "verify-checkpoint", tok]) == 1
            assert audit.main(["audit.py", "verify-checkpoint", tok, str(real_jwks_file)]) == 1
            assert audit.main(["audit.py", "verify-checkpoint", tok, real_url]) == 1

        assert quotes.main(["quotes.py", "verify", q.jws, str(foreign_jwks_file)]) == 1
        assert quotes.main(["quotes.py", "verify", q.jws, foreign_url]) == 1
        assert audit.main(["audit.py", "verify-checkpoint", cp["jws"], str(foreign_jwks_file)]) == 1
        assert audit.main(["audit.py", "verify-checkpoint", cp["jws"], foreign_url]) == 1
        capsys.readouterr()


def test_cli_rejects_alg_confusion_and_missing_or_bad_kid_with_exit_code_1(tmp_path, capsys):
    q = _issue_quote(agent_id="alg-target")
    cp = audit.checkpoint()
    keys.rotate()
    q_rot = _issue_quote(agent_id="alg-target-rot")
    cp_rot = audit.checkpoint()

    _, q_payload_bytes, _, _, q_p, _ = _split_jws(q_rot.jws)
    _, cp_payload_bytes, _, _, cp_p, _ = _split_jws(cp_rot["jws"])
    active_kid = keys.kid()
    pub = keys.public_jwk()

    secrets = [
        _b64u_decode(pub["x"]),
        json.dumps(pub, sort_keys=True).encode("utf-8"),
        active_kid.encode("utf-8"),
    ]

    alg_confused_quotes = [
        f"{_b64u_json({'alg': 'none', 'kid': active_kid})}.{q_p}.",
        f"{_b64u_json({'alg': 'none'})}.{q_p}.AAAA",
        f"{_b64u_json({'alg': 'RS256', 'kid': active_kid})}.{q_p}.AAAA",
        f"{_b64u_json({'alg': 'Ed25519', 'kid': active_kid})}.{q_p}.AAAA",
    ]
    alg_confused_cps = [
        f"{_b64u_json({'alg': 'none', 'kid': active_kid})}.{cp_p}.",
        f"{_b64u_json({'alg': 'none'})}.{cp_p}.AAAA",
        f"{_b64u_json({'alg': 'RS256', 'kid': active_kid})}.{cp_p}.AAAA",
        f"{_b64u_json({'alg': 'Ed25519', 'kid': active_kid})}.{cp_p}.AAAA",
    ]

    for secret in secrets:
        qh = _b64u_json({"alg": "HS256", "kid": active_kid})
        qsig = _b64u_encode(hmac.new(secret, f"{qh}.{q_p}".encode("ascii"), hashlib.sha256).digest())
        alg_confused_quotes.append(f"{qh}.{q_p}.{qsig}")

        ch = _b64u_json({"alg": "HS256", "kid": active_kid})
        csig = _b64u_encode(hmac.new(secret, f"{ch}.{cp_p}".encode("ascii"), hashlib.sha256).digest())
        alg_confused_cps.append(f"{ch}.{cp_p}.{csig}")

    q_no_kid = jws.serialize_compact({"alg": "EdDSA"}, q_payload_bytes, keys.signing_key(), algorithms=["EdDSA"])
    q_empty_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": ""}, q_payload_bytes, keys.signing_key(), algorithms=["EdDSA"]
    )
    q_unknown_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": "nonexistent-kid"}, q_payload_bytes, keys.signing_key(), algorithms=["EdDSA"]
    )

    cp_no_kid = jws.serialize_compact({"alg": "EdDSA"}, cp_payload_bytes, keys.signing_key(), algorithms=["EdDSA"])
    cp_empty_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": ""}, cp_payload_bytes, keys.signing_key(), algorithms=["EdDSA"]
    )
    cp_unknown_kid = jws.serialize_compact(
        {"alg": "EdDSA", "kid": "nonexistent-kid"}, cp_payload_bytes, keys.signing_key(), algorithms=["EdDSA"]
    )

    jwks_file = tmp_path / "rotated-jwks.json"
    jwks_file.write_text(json.dumps(keys.jwks()), encoding="utf-8")

    with _serve_jwks() as (base_url, _):
        jwks_url = f"{base_url}/.well-known/jwks.json"
        assert quotes.main(["quotes.py", "verify", q.jws, jwks_url]) == 0
        assert audit.main(["audit.py", "verify-checkpoint", cp["jws"], jwks_url]) == 0

        for tok in [*alg_confused_quotes, q_no_kid, q_empty_kid, q_unknown_kid]:
            assert quotes.main(["quotes.py", "verify", tok]) == 1
            assert quotes.main(["quotes.py", "verify", tok, str(jwks_file)]) == 1
            assert quotes.main(["quotes.py", "verify", tok, jwks_url]) == 1

        for tok in [*alg_confused_cps, cp_no_kid, cp_empty_kid, cp_unknown_kid]:
            assert audit.main(["audit.py", "verify-checkpoint", tok]) == 1
            assert audit.main(["audit.py", "verify-checkpoint", tok, str(jwks_file)]) == 1
            assert audit.main(["audit.py", "verify-checkpoint", tok, jwks_url]) == 1

        capsys.readouterr()


def test_cli_rejects_malformed_signed_payloads_with_exit_code_1(tmp_path, capsys):
    header = {"alg": "EdDSA", "kid": keys.kid()}
    sk = keys.signing_key()

    malformed_quote_payloads = [
        b"not-valid-json{",
        b"",
        b'{"quote_id":"q_missing_fields"}',
        b'{"quote_id":"q_bad_types","customer":"hotel-aurora","total_net":"not-a-float"}',
    ]
    malformed_cp_payloads = [
        b"not-valid-json{",
        b"",
        b'{"seq":1}',
        b'{"hash":"abc"}',
        b"[1,2,3]",
        b"null",
    ]

    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps(keys.jwks()), encoding="utf-8")

    with _serve_jwks() as (base_url, _):
        jwks_url = f"{base_url}/.well-known/jwks.json"

        for raw in malformed_quote_payloads:
            tok = jws.serialize_compact(header, raw, sk, algorithms=["EdDSA"])
            for extra in ([], [str(jwks_file)], [jwks_url]):
                rc = quotes.main(["quotes.py", "verify", tok, *extra])
                out = capsys.readouterr().out
                assert rc == 1
                assert "BAD_SIGNATURE" in out

        for raw in malformed_cp_payloads:
            tok = jws.serialize_compact(header, raw, sk, algorithms=["EdDSA"])
            for extra in ([], [str(jwks_file)], [jwks_url]):
                rc = audit.main(["audit.py", "verify-checkpoint", tok, *extra])
                out = capsys.readouterr().out
                assert rc == 1
                assert "BAD_SIGNATURE" in out


@pytest.mark.parametrize(
    "quotes_argv,audit_argv",
    [
        (["quotes.py"], ["audit.py"]),
        (["quotes.py", "verify"], ["audit.py", "verify-checkpoint"]),
        (["quotes.py", "issue", "dummy"], ["audit.py", "checkpoint", "dummy"]),
        (["quotes.py", "--help"], ["audit.py", "--help"]),
    ],
)
def test_cli_bad_args_return_exit_code_2(quotes_argv, audit_argv, capsys):
    assert quotes.main(quotes_argv) == 2
    q_out = capsys.readouterr().out
    assert "usage: python -m aimo.quotes verify" in q_out

    assert audit.main(audit_argv) == 2
    a_out = capsys.readouterr().out
    assert "usage: python -m aimo.audit verify-checkpoint" in a_out


def test_multi_key_dir_isolation_in_process(tmp_path, monkeypatch):
    dir_a = tmp_path / "keys_a"
    dir_b = tmp_path / "keys_b"

    monkeypatch.setenv("AIMO_KEY_DIR", str(dir_a))
    kid_a = keys.kid()
    jwks_a = keys.jwks()
    q_a = _issue_quote(agent_id="agent-dir-a")
    cp_a = audit.checkpoint()

    monkeypatch.setenv("AIMO_KEY_DIR", str(dir_b))
    kid_b = keys.kid()
    jwks_b = keys.jwks()
    q_b = _issue_quote(agent_id="agent-dir-b")
    cp_b = audit.checkpoint()

    assert kid_a != kid_b
    assert len(keys._cache) == 2

    with pytest.raises(quotes.QuoteError) as q_err:
        quotes.verify(q_a.jws)
    assert q_err.value.code == "BAD_SIGNATURE"
    with pytest.raises(ValueError, match="BAD_SIGNATURE"):
        audit.verify_checkpoint(cp_a["jws"])

    assert quotes.verify(q_b.jws).quote_id == q_b.quote_id
    assert quotes.verify(q_a.jws, jwks_a).quote_id == q_a.quote_id
    assert quotes.verify(q_b.jws, jwks_b).quote_id == q_b.quote_id

    monkeypatch.setenv("AIMO_KEY_DIR", str(dir_a))
    keys._cache.clear()
    assert keys.kid() == kid_a
    assert quotes.verify(q_a.jws).quote_id == q_a.quote_id
    assert audit.verify_checkpoint(cp_a["jws"])["hash"] == cp_a["hash"]


def test_cross_type_quote_and_checkpoint_token_confusion_rejected(capsys):
    q = _issue_quote(agent_id="domain-sep")
    audit.record("hotel-aurora", "system", "order.placed", "o_dom", "Domain separation check.")
    cp = audit.checkpoint()

    q_header, _, _, _, _, _ = _split_jws(q.jws)
    cp_header, _, _, _, _, _ = _split_jws(cp["jws"])
    assert q_header == cp_header == {"alg": "EdDSA", "kid": keys.kid()}

    with pytest.raises(quotes.QuoteError) as q_err:
        quotes.verify(cp["jws"])
    assert q_err.value.code == "BAD_SIGNATURE"

    with pytest.raises(quotes.QuoteError) as red_err:
        quotes.redeem(q.quote_id, cp["jws"], q.customer, q.agent_id)
    assert red_err.value.code == "BAD_SIGNATURE"

    with pytest.raises(ValueError, match="BAD_SIGNATURE"):
        audit.verify_checkpoint(q.jws)

    with _serve_jwks() as (base_url, _):
        jwks_url = f"{base_url}/.well-known/jwks.json"
        assert quotes.main(["quotes.py", "verify", cp["jws"], jwks_url]) == 1
        assert audit.main(["audit.py", "verify-checkpoint", q.jws, jwks_url]) == 1
        capsys.readouterr()


@pytest.mark.parametrize(
    "tz_factory",
    [
        lambda now: (now + timedelta(hours=3)).replace(tzinfo=None),
        lambda now: (now + timedelta(hours=3)).astimezone(quotes.HELSINKI),
        lambda now: now + timedelta(hours=3),
    ],
)
def test_quote_timestamp_variants_canonical_roundtrip(tz_factory):
    now = datetime.now(timezone.utc)
    valid_until = tz_factory(now)
    delivery = datetime.now(quotes.HELSINKI).date() + timedelta(days=2)
    line = QuoteLine(
        product_id="400500",
        name="Valio Oivariini Laktoositon 500 g",
        quantity=5.0,
        sales_unit="PCE",
        unit_price_net=3.20,
        line_total_net=16.00,
    )
    q = quotes.issue("hotel-aurora", "tz-agent", [line], delivery, valid_until, 18.24)
    assert q.valid_until.tzinfo is not None

    from_db = quotes.get(q.quote_id)
    assert from_db == q

    ks = KeySet.import_key_set(keys.jwks())
    deserialized = jws.deserialize_compact(q.jws, ks, algorithms=["EdDSA"])
    raw_str = deserialized.payload.decode("utf-8")
    assert raw_str == json.dumps(json.loads(raw_str), sort_keys=True, separators=(",", ":"))
    assert Quote(**json.loads(raw_str), jws=q.jws) == q


def test_keys_rotate_cli_cold_start_and_three_rotations(tmp_path, monkeypatch):
    fresh_dir = tmp_path / "cli_keys"
    monkeypatch.setenv("AIMO_KEY_DIR", str(fresh_dir))
    keys._cache.clear()
    assert not fresh_dir.exists()

    root = str(Path(__file__).resolve().parents[1])
    existing_pypath = os.environ.get("PYTHONPATH", "")
    pypath = root if not existing_pypath else f"{root}{os.pathsep}{existing_pypath}"
    env = {**os.environ, "PYTHONPATH": pypath}

    rotated_kids = []
    for _ in range(3):
        res = subprocess.run(
            [sys.executable, "-m", "aimo.keys", "rotate"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        assert res.stdout.startswith("new kid ")
        rotated_kids.append(res.stdout.strip().split()[-1])

    keys._cache.clear()
    assert keys.kid() == rotated_kids[-1]
    jwks_doc = keys.jwks()
    assert len(jwks_doc["keys"]) == 4
    assert jwks_doc["keys"][0]["kid"] == rotated_kids[-1]
    for entry in jwks_doc["keys"]:
        _assert_public_jwk_hygiene(entry)

