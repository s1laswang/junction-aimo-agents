#!/bin/bash
# Wind tunnel pipeline sweep on FR8 (offline: compute nodes have no internet; everything replays the snapshot).
# One job, 8 cores, 8 shards in parallel (CEO rule: <=2 concurrent jobs, <=8 cores each, always --time).
#   sbatch --export=ALL,WT_N=8000 windtunnel/slurm/sweep.sbatch
#SBATCH --job-name=aimo-wt-sweep
#SBATCH --partition=normal
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --time=00:45:00
#SBATCH --output=%x-%j.out
set -euo pipefail
cd "$HOME/junction-aimo-agents"
export AIMO_DATA_DIR="$HOME/junction-aimo-agents-data"
export WT_DIR="$AIMO_DATA_DIR/windtunnel"
export AIMO_OFFLINE=1 AIMO_RECIPE_CHECK=0 AIMO_SCREEN_GEMINI=0 UV_OFFLINE=1
N=${WT_N:-8000}; SHARDS=${WT_SHARDS:-8}; SEED0=${WT_SEED0:-100000}; TAG=${WT_TAG:-sweep$N}
PER=$(( (N + SHARDS - 1) / SHARDS ))
OUT="$WT_DIR/$TAG"; mkdir -p "$OUT"
echo "job $SLURM_JOB_ID on $(hostname): N=$N shards=$SHARDS per=$PER out=$OUT git=$(cat .git_rev 2>/dev/null || echo ?)"
for i in $(seq 0 $((SHARDS-1))); do
  (
    S="$OUT/scen_$i.jsonl"
    .venv/bin/python -m windtunnel.generate --n "$PER" --seed $((SEED0 + i*PER)) --out "$S" > "$OUT/gen_$i.log" 2>&1
    for SUT in team baseline; do
      .venv/bin/python -m windtunnel.run pipeline --scenarios "$S" --sut $SUT --out "$OUT/run_${SUT}_$i.jsonl" >> "$OUT/run_$i.log" 2>&1
    done
  ) &
done
wait
cat "$OUT"/scen_*.jsonl > "$OUT/scenarios.jsonl"
.venv/bin/python -m windtunnel.score --scenarios "$OUT/scenarios.jsonl" \
  --runs "$OUT/run_team_*.jsonl" "$OUT/run_baseline_*.jsonl" --labels team baseline --name "$TAG" \
  --md "$OUT/windtunnel-results.md"
echo done
