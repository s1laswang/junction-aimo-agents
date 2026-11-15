# Warehouse Arena Pixel-Art Asset Ledger (`CC0 1.0 Universal`)

This document records the provenance, license verification, and sprite inventory for the playable **"VALIO: TRY TO SCAM THE AGENT"** wholesale warehouse arena (`docs/arena/`).

In accordance with the two-step asset provenance gate, this ledger is committed **before** vendoring any PNG files into `docs/arena/assets/`.

## Provenance & Clean-Room Attestation

- **0 paid, ripped, or commercial game sprites:** No sprites, tiles, palettes, or UI frames are copied or derived from commercial games, proprietary asset stores, or restricted bundles.
- **0 look-alikes or copyrighted IP:** No characters, monsters, logos, or tiles imitate Nintendo, Pokemon, Stardew Valley, Undertale, RPG Maker RTP, or any third-party trademarked franchise.
- **100% Public Domain (`CC0 1.0 Universal`):** Every sprite planned for `docs/arena/assets/` is extracted unmodified from official archives published by **Kenney Vleugels (`Kenney.nl`)** under **Creative Commons Zero (`CC0 1.0 Universal`)**, verified both on the live asset page (`HTTP 200`) and inside each archive's `License.txt`.

## Verified Source Packs

| Pack ID | Pack Name | Exact Source URL | Author | License | Archive `License.txt` Verification | Official Zip Size |
|---|---|---|---|---|---|---:|
| `tiny-dungeon` | `Tiny Dungeon (1.0)` | https://kenney.nl/assets/tiny-dungeon | Kenney Vleugels (Kenney.nl) | `CC0 1.0 Universal` (`CC0-1.0`, https://creativecommons.org/publicdomain/zero/1.0/) | `Tiny Dungeon (1.0)` / `Created/distributed by Kenney (www.kenney.nl)` / `License: (Creative Commons Zero, CC0) http://creativecommons.org/publicdomain/zero/1.0/` | 98,530 B |
| `tiny-town` | `Tiny Town (1.1)` | https://kenney.nl/assets/tiny-town | Kenney Vleugels (Kenney.nl) | `CC0 1.0 Universal` (`CC0-1.0`, https://creativecommons.org/publicdomain/zero/1.0/) | `Tiny Town (1.1)` / `Created/distributed by Kenney (www.kenney.nl)` / `License: (Creative Commons Zero, CC0) http://creativecommons.org/publicdomain/zero/1.0/` | 182,243 B |
| `emotes-pack` | `Emotes Pack` | https://kenney.nl/assets/emotes-pack | Kenney Vleugels (Kenney.nl) | `CC0 1.0 Universal` (`CC0-1.0`, https://creativecommons.org/publicdomain/zero/1.0/) | `Emotes` / `by Kenney Vleugels (Kenney.nl)` / `License (Creative Commons Zero, CC0) http://creativecommons.org/publicdomain/zero/1.0/` | 380,191 B |
| `pixel-platformer` | `Pixel Platformer (1.2)` | https://kenney.nl/assets/pixel-platformer | Kenney Vleugels (Kenney.nl) | `CC0 1.0 Universal` (`CC0-1.0`, https://creativecommons.org/publicdomain/zero/1.0/) | `Pixel Platformer (1.2)` / `Created/distributed by Kenney (www.kenney.nl)` / `License: (Creative Commons Zero, CC0) http://creativecommons.org/publicdomain/zero/1.0/` | 260,312 B |

## Selective Sprite Inventory (Step 1 Plan: 24 PNGs)

Only the 24 specific top-down warehouse tiles, character sprites, and status emotes needed by the arena are selected for vendoring (well below the 1.5 MB target and 15 MB repository cap).

| Semantic Key | Target Path | Dimensions | Bytes | Source Pack & Archive Member | SHA-256 (first 16) | Arena Role |
|---|---|---|---:|---|---|---|
| `characters.supplier` | `docs/arena/assets/supplier.png` | 16x16 | 228 | `Tiny Dungeon (1.0)` (`Tiles/tile_0111.png`) | `8ecfc9b753e676a3` | Rogue supplier (prompt-injection and price-drift attacker) |
| `characters.colleague` | `docs/arena/assets/colleague.png` | 16x16 | 237 | `Tiny Dungeon (1.0)` (`Tiles/tile_0112.png`) | `1e82481db9d95d0c` | Sneaky colleague (PIN bypass and self-approval attacker) |
| `characters.buyer` | `docs/arena/assets/buyer.png` | 16x16 | 224 | `Tiny Dungeon (1.0)` (`Tiles/tile_0086.png`) | `0606653ad07e177c` | Kitchen head buyer in apron |
| `characters.agent` | `docs/arena/assets/agent.png` | 16x16 | 206 | `Tiny Dungeon (1.0)` (`Tiles/tile_0096.png`) | `b8a0a9376c1c3398` | Valio Aimo Trusted Trade Agent (visored steel guard) |
| `characters.approver` | `docs/arena/assets/approver.png` | 16x16 | 238 | `Tiny Dungeon (1.0)` (`Tiles/tile_0099.png`) | `61af4b9f1f67aa76` | Human procurement approver Anna |
| `tiles.floor` | `docs/arena/assets/floor.png` | 16x16 | 142 | `Tiny Dungeon (1.0)` (`Tiles/tile_0042.png`) | `8dc63d1b4fc05e9e` | Paved warehouse floor tile |
| `tiles.floor_alt` | `docs/arena/assets/floor_alt.png` | 16x16 | 99 | `Tiny Town (1.1)` (`Tiles/tile_0109.png`) | `d1ff475a1ad0d4fd` | Cold-room slate floor tile |
| `tiles.wall` | `docs/arena/assets/wall.png` | 16x16 | 128 | `Tiny Dungeon (1.0)` (`Tiles/tile_0040.png`) | `7541f8325e46dc4d` | Stone brick warehouse perimeter wall |
| `tiles.crate` | `docs/arena/assets/crate.png` | 16x16 | 167 | `Tiny Dungeon (1.0)` (`Tiles/tile_0063.png`) | `2c7d1a1fdf12a6cd` | Stacked wooden wholesale crates |
| `tiles.pallet` | `docs/arena/assets/pallet.png` | 16x16 | 213 | `Tiny Town (1.1)` (`Tiles/tile_0057.png`) | `b3fc5ffa2c39365a` | Wooden cargo pallet / hand-cart |
| `tiles.barrel` | `docs/arena/assets/barrel.png` | 16x16 | 207 | `Tiny Dungeon (1.0)` (`Tiles/tile_0082.png`) | `2efb31e30cd6f152` | Cold-storage dairy drum / barrel |
| `tiles.desk` | `docs/arena/assets/desk.png` | 16x16 | 163 | `Tiny Dungeon (1.0)` (`Tiles/tile_0072.png`) | `a977172d74aef292` | Procurement intake desk / counter |
| `tiles.gate` | `docs/arena/assets/gate.png` | 16x16 | 165 | `Tiny Dungeon (1.0)` (`Tiles/tile_0076.png`) | `349c56fdf06e20fb` | Security gate barrier |
| `tiles.door` | `docs/arena/assets/door.png` | 16x16 | 191 | `Tiny Dungeon (1.0)` (`Tiles/tile_0045.png`) | `4f5d0fd34c70872b` | Loading bay double door |
| `tiles.vault` | `docs/arena/assets/vault.png` | 16x16 | 196 | `Tiny Dungeon (1.0)` (`Tiles/tile_0089.png`) | `0b9fa753068061e8` | Locked Ed25519 audit ledger vault |
| `tiles.terminal` | `docs/arena/assets/terminal.png` | 16x16 | 201 | `Tiny Dungeon (1.0)` (`Tiles/tile_0074.png`) | `cfb675c1dcc7530a` | Order and quote verification terminal |
| `icons.blocked` | `docs/arena/assets/blocked.png` | 16x16 | 185 | `Emotes Pack` (`PNG/Pixel/Style 1/emote_cross.png`) | `89a8d0c3d91544ff` | Hard-block X icon (allergen / injection / auth rejected) |
| `icons.verified` | `docs/arena/assets/verified.png` | 16x16 | 154 | `Tiny Dungeon (1.0)` (`Tiles/tile_0102.png`) | `35af44a6fc76a09e` | Steel shield icon (Ed25519 signature and policy verified) |
| `icons.alert` | `docs/arena/assets/alert.png` | 16x16 | 174 | `Emotes Pack` (`PNG/Pixel/Style 1/emote_exclamation.png`) | `5111d386466a77bc` | Red exclamation security / stockout alert |
| `icons.quote` | `docs/arena/assets/quote.png` | 16x16 | 189 | `Tiny Town (1.1)` (`Tiles/tile_0083.png`) | `07b9cf1d4c066f91` | Signed quote / order specification board |
| `icons.lock` | `docs/arena/assets/lock.png` | 18x18 | 170 | `Pixel Platformer (1.2)` (`Tiles/tile_0028.png`) | `6654c4b47deb751e` | Human-approver PIN / session lock |
| `icons.key` | `docs/arena/assets/key.png` | 16x16 | 172 | `Tiny Town (1.1)` (`Tiles/tile_0117.png`) | `d2be7fbb7b9c2a27` | Approver signing key |
| `icons.coin` | `docs/arena/assets/coin.png` | 16x16 | 162 | `Tiny Town (1.1)` (`Tiles/tile_0093.png`) | `d432578df64a8e4c` | Gold unit-price coin |
| `icons.cash` | `docs/arena/assets/cash.png` | 16x16 | 171 | `Emotes Pack` (`PNG/Pixel/Style 1/emote_cash.png`) | `6cb39c38d9bda3cb` | Budget / price-drift $ emote |

## Footprint Summary (Step 1 Pre-Vendoring)

- **Planned PNG sprite count:** 24 files (`4,382 bytes` total across PNGs)
- **Vendored PNGs in repository at Step 1:** 0 files (`0 bytes`; vendored in Step 2 together with `docs/arena/assets/manifest.json`)
- **Hard budget check:** `< 1.5 MB` target and `< 15 MB` repository ceiling (`4,382 bytes` PNG payload is `0.03%` of the 15 MB cap)
