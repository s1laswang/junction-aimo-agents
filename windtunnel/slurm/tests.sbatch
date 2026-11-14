#!/bin/bash
# Full pytest suite on FR8 (offline): unit + e2e (TestClient) + full wind tunnel scenario suite (WT_FULL=1).
#   sbatch windtunnel/slurm/tests.sbatch
#SBATCH --job-name=aimo-wt-tests
#SBATCH --partition=normal
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=%x-%j.out
set -euo pipefail
cd "$HOME/junction-aimo-agents"
export AIMO_DATA_DIR="$HOME/junction-aimo-agents-data"
export WT_SNAPSHOT_DIR="$AIMO_DATA_DIR/windtunnel/snapshot"
export WT_DIR="$AIMO_DATA_DIR/windtunnel/pytest"
export AIMO_OFFLINE=1 AIMO_RECIPE_CHECK=0 AIMO_SCREEN_GEMINI=0 WT_FULL=1 WT_SUITE_N=${WT_SUITE_N:-640}
echo "job $SLURM_JOB_ID on $(hostname) git=$(cat .git_rev 2>/dev/null || echo ?)"
.venv/bin/python -m pytest -q -rs tests 2>&1 | tail -60
