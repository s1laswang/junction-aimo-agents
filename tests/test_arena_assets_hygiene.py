from __future__ import annotations

import hashlib
import io
import json
import pathlib
import struct
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS_DOC = ROOT / "docs" / "demo" / "ASSETS.md"
ASSETS_DIR = ROOT / "docs" / "arena" / "assets"
MANIFEST_PATH = ASSETS_DIR / "manifest.json"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
EM_DASH = chr(0x2014)
HASH_CHAR = chr(35)

REQUIRED_GROUPS = {
    "characters": ("supplier", "colleague", "buyer", "agent", "approver"),
    "tiles": ("floor", "wall", "crate", "pallet", "desk", "gate", "vault", "terminal"),
    "icons": ("blocked", "verified", "alert", "quote", "lock", "coin"),
}


def _load_manifest() -> dict:
    assert MANIFEST_PATH.is_file(), f"Missing manifest: {MANIFEST_PATH}"
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


def _read_png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    assert len(png_bytes) >= 24
    assert png_bytes[:8] == PNG_MAGIC
    ihdr_len, ihdr_type = struct.unpack(">I4s", png_bytes[8:16])
    assert ihdr_len == 13
    assert ihdr_type == b"IHDR"
    width, height = struct.unpack(">II", png_bytes[16:24])
    assert width > 0 and height > 0
    return width, height


def test_assets_ledger_exists_and_attests_cc0_provenance():
    assert ASSETS_DOC.is_file(), f"Missing {ASSETS_DOC}"
    doc = ASSETS_DOC.read_text(encoding="utf-8")
    assert doc.strip()
    assert EM_DASH not in doc

    for required_phrase in (
        "Kenney Vleugels (Kenney.nl)",
        "CC0 1.0 Universal",
        "CC0-1.0",
        "https://creativecommons.org/publicdomain/zero/1.0/",
        "License.txt",
        "0 paid, ripped, or commercial game sprites",
        "0 look-alikes",
    ):
        assert required_phrase in doc, f"ASSETS.md missing {required_phrase!r}"

    manifest = _load_manifest()
    packs = manifest.get("packs", {})
    assert len(packs) >= 4
    for pack_id, pack_meta in packs.items():
        url = pack_meta["url"]
        name = pack_meta["name"]
        author = pack_meta["author"]
        assert url.startswith("https://kenney.nl/assets/"), f"Unexpected source URL for {pack_id}: {url}"
        assert pack_meta["license"] == "CC0-1.0"
        assert author == "Kenney Vleugels (Kenney.nl)"
        assert url in doc, f"Pack URL {url} missing from ASSETS.md"
        assert name in doc, f"Pack name {name} missing from ASSETS.md"
        assert author in doc


def test_manifest_resolves_every_png_with_valid_header_and_dimensions():
    manifest = _load_manifest()
    doc = ASSETS_DOC.read_text(encoding="utf-8")
    assert manifest.get("license") == "CC0-1.0"

    seen_files: set[str] = set()
    total_png_bytes = 0

    for group, required_keys in REQUIRED_GROUPS.items():
        group_map = manifest.get(group)
        assert isinstance(group_map, dict), f"Missing group {group!r} in manifest.json"
        for req_key in required_keys:
            assert req_key in group_map, f"Missing semantic key {group}.{req_key} in manifest.json"

        for key, entry in group_map.items():
            rel_file = entry["file"]
            assert isinstance(rel_file, str) and rel_file.endswith(".png")
            png_path = (ASSETS_DIR / rel_file).resolve()
            assert png_path.is_relative_to(ASSETS_DIR.resolve())
            assert png_path.is_file(), f"Missing PNG for {group}.{key}: {png_path}"

            raw_png = png_path.read_bytes()
            width, height = _read_png_dimensions(raw_png)
            assert width == entry["width"], f"Width mismatch on {rel_file}"
            assert height == entry["height"], f"Height mismatch on {rel_file}"
            assert entry["license"] == "CC0-1.0"
            assert isinstance(entry["pack"], str) and entry["pack"]

            if "bytes" in entry:
                assert len(raw_png) == entry["bytes"], f"Byte count mismatch on {rel_file}"
            if "sha256_16" in entry:
                digest_16 = hashlib.sha256(raw_png).hexdigest()[:16]
                assert digest_16 == entry["sha256_16"], f"SHA-256 mismatch on {rel_file}"

            assert f"`{group}.{key}`" in doc, f"Semantic key {group}.{key} missing from ASSETS.md"
            assert f"`docs/arena/assets/{rel_file}`" in doc, f"Path {rel_file} missing from ASSETS.md"

            if rel_file not in seen_files:
                seen_files.add(rel_file)
                total_png_bytes += len(raw_png)

    disk_pngs = {p.name for p in ASSETS_DIR.glob("*.png")}
    assert disk_pngs == seen_files, f"Unindexed or missing PNGs: {disk_pngs ^ seen_files}"
    assert len(seen_files) >= 19
    assert f"{total_png_bytes:,} bytes" in doc


def test_arena_assets_directory_stays_well_under_size_budget():
    assert ASSETS_DIR.is_dir()
    files = [p for p in ASSETS_DIR.rglob("*") if p.is_file()]
    assert len(files) >= 20
    total_bytes = sum(p.stat().st_size for p in files)
    assert total_bytes > 0
    assert total_bytes < 1_500_000, f"Arena assets exceeded 1.5 MB target: {total_bytes}"
    assert total_bytes < 5_000_000, f"Arena assets exceeded 5 MB hygiene threshold: {total_bytes}"
    assert total_bytes < 15_000_000, f"Arena assets exceeded 15 MB hard cap: {total_bytes}"

    doc = ASSETS_DOC.read_text(encoding="utf-8")
    assert f"{total_bytes:,} bytes" in doc


def test_this_test_file_has_zero_comments_and_zero_em_dashes():
    self_path = pathlib.Path(__file__).resolve()
    raw = self_path.read_text(encoding="utf-8")
    assert EM_DASH not in raw
    assert HASH_CHAR not in raw
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(raw).readline)
        if tok.type == tokenize.COMMENT
    ]
    assert comments == []
