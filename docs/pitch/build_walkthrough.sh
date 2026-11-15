#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
F=docs/reports/frontend
OUT=docs/pitch/demo-walkthrough.mp4
VOICE="${VOICE:-Samantha}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

frames=(
  "$F/live-plan.png|A hotel's buyer agent, running on Gemini, gets one sentence: we need one thousand litres of lactose-free milk by Monday. It turns it into a structured need and works only through our open M C P tools."
  "$F/1080p-plan.png|The usual ten-litre box missed its order cut-off, so the plan picks the same Valio milk in one-litre cartons for one thousand and ten euros, and explains every option it rejected. This frame is the interface's offline mode."
  "$F/live-plan.png|The hotel's own policy says three things need a human: the total is over the five hundred euro limit, the pack is a swap, and the volume is three point three times the usual week. The agent pauses."
  "$F/s5-ops-blocked-attacks.png|The agent tries to approve its own order, naming the real manager and the correct quote. The gateway refuses with four oh three, and the attempt is written into the signed audit log as a blocked attack."
  "$F/s5-signin-panel.png|Approvals are human only. Anna, the procurement manager, signed in with her own passphrase before she started the run, and that one sign-in covers her approval. No agent ever receives it."
  "$F/s5-approval-signed-in.png|She approves this exact signed quote. If the price or the lines change, her approval no longer applies."
  "$F/live-run-approved.png|The order is placed, and the Peppol style order response says: accepted."
  "$F/live-audit-verified.png|Every request, decision and refusal is in a hash-chained, signed audit log, and verify recomputes all of it."
  "$F/live-tamper-red.png|If anyone edits an event afterwards, verification turns red at exactly that event."
  "$F/live-restore-green.png|Restore the event, and the chain verifies again."
  "$F/s5-clarifying-question.png|When a request is ambiguous, the agent asks one clarifying question instead of guessing."
  "$F/s5-photo-intake-presenter.png|A kitchen can send a photo of a handwritten order. Gemini reads it, and the same guardrails apply."
  "$F/s5-voice-confirm.png|An approver can also decide by voice. The transcript is shown, and one confirm click sends the normal signed-in decision. Trusted Trade Agents: open, signed, and a human in control."
)

i=0
: > "$TMP/list.txt"
for entry in "${frames[@]}"; do
  img="${entry%%|*}"; text="${entry#*|}"
  i=$((i + 1)); n=$(printf "%02d" "$i")
  [ -f "$img" ] || { echo "missing frame: $img" >&2; exit 1; }
  say -v "$VOICE" -r 172 -o "$TMP/$n.aiff" "$text"
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/$n.aiff")
  len=$(python3 -c "print(round($dur + 0.5, 2))")
  ffmpeg -v error -y -loop 1 -framerate 10 -i "$img" -i "$TMP/$n.aiff" \
    -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p" \
    -af "apad" -t "$len" -c:v libx264 -tune stillimage -preset veryfast -crf 26 -r 10 \
    -c:a aac -b:a 96k -ar 44100 -ac 1 "$TMP/$n.mp4"
  echo "file '$TMP/$n.mp4'" >> "$TMP/list.txt"
  echo "frame $n  ${len}s  $(basename "$img")"
done
ffmpeg -v error -y -f concat -safe 0 -i "$TMP/list.txt" -c copy -movflags +faststart "$OUT"
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "$OUT"
