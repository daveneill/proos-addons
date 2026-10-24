#!/bin/bash
# ProOS AI Test 0.2.0 (register 976). The three spoken takes from the G-Flip microphone test (24 Oct; L1 boost off /
# capture 35 %, L2 boost off / 60 %, L3 boost +10 dB / 35 %) heard by each Whisper size on the graphics chip, and the
# smallest and largest on the processor too. What was read aloud:
REF="Hey Jarvis, turn the office light on and play some music in the kitchen. What is the weather like in Melbourne tomorrow?"
OUT=/config/www/proos_ai_test.txt
mkdir -p /config/www /data/models /tmp/takes
: > "$OUT"
log() { echo "$@" | tee -a "$OUT"; }
log "ProOS AI test 0.2.0 — speech — $(date -u '+%Y-%m-%d %H:%M UTC')"
log "read aloud: $REF"
vulkaninfo --summary 2>&1 | grep -m2 -E "deviceName" | tee -a "$OUT"
for f in /takes/*.wav; do ffmpeg -loglevel error -y -i "$f" -ar 16000 -ac 1 "/tmp/takes/$(basename "$f")"; done
CLI=$(command -v whisper-cli)
get() {
  [ -s "/data/models/$1" ] && return 0
  curl -sSL --retry 3 -o "/data/models/$1.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$1" \
    && mv "/data/models/$1.part" "/data/models/$1"
  log "downloaded $1 $(du -h "/data/models/$1" | cut -f1)"
}
run() {   # model, where (gpu|cpu)
  local flag=""; [ "$2" = cpu ] && flag="-ng"
  log ""; log "== $1 — $([ "$2" = cpu ] && echo processor || echo 'graphics chip')"
  for t in /tmp/takes/*.wav; do
    local start=$(date +%s%N)
    local text=$("$CLI" -m "/data/models/$1" -f "$t" -l en -nt -t 8 $flag 2>/dev/null | tr -s ' \n' ' ' | sed 's/^ //')
    local ms=$(( ($(date +%s%N) - start) / 1000000 ))
    log "$(basename "$t" .wav | sed 's/speech_1_0_//')  $((ms/1000)).$(printf '%03d' $((ms%1000)))s  \"$text\""
  done
}
for m in ggml-base.en.bin ggml-small.en.bin ggml-medium.en.bin ggml-large-v3-turbo.bin; do get "$m"; done
for m in ggml-base.en.bin ggml-small.en.bin ggml-medium.en.bin ggml-large-v3-turbo.bin; do run "$m" gpu; done
run ggml-base.en.bin cpu
run ggml-large-v3-turbo.bin cpu
log ""; log "DONE"
exec sleep infinity
