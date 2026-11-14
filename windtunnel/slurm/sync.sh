#!/bin/bash
# Laptop -> freighter: code (no .env, no .venv, no data), then the data the offline runs need.
# Repo is private and freighter has no GitHub credentials, so rsync instead of git clone.
#   bash windtunnel/slurm/sync.sh          # code + data
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SSH="ssh -o ConnectTimeout=20"
cd "$ROOT"
git rev-parse --short HEAD > .git_rev
rsync -az -e "$SSH" --exclude .venv --exclude .claude --exclude data --exclude .env --exclude __pycache__ \
  --exclude .git ./ freighter:junction-aimo-agents/
# data: raw product files + catalog artifacts + wind tunnel snapshot (dereference symlinks)
rsync -azL -e "$SSH" data/valio_aimo_junction26_product_data.json data/valio_aimo_junction26_delivery_reliability.csv \
  data/valio_aimo_junction26_carbon_footprint.csv data/catalog.sqlite data/embeddings.npy data/embeddings_ids.json \
  freighter:junction-aimo-agents-data/
rsync -azL -e "$SSH" data/windtunnel/snapshot freighter:junction-aimo-agents-data/windtunnel/
if [ -d data/cache ]; then rsync -azL -e "$SSH" data/cache freighter:junction-aimo-agents-data/; fi
$SSH freighter 'cd ~/junction-aimo-agents && ~/.local/bin/uv sync --extra dev -q && echo synced $(cat .git_rev)'
