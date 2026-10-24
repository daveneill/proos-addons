#!/bin/bash
# ProOS AI Test (register 976). One run: what graphics chip the add-on sees, then each model timed on the graphics chip
# and on the processor alone. Results to /config/www/proos_ai_test.txt (served as /local/proos_ai_test.txt).
OUT=/config/www/proos_ai_test.txt
mkdir -p /config/www /data/models
: > "$OUT"
log() { echo "$@" | tee -a "$OUT"; }
log "ProOS AI test — $(date -u '+%Y-%m-%d %H:%M UTC')"
log "== processor: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2) · $(nproc) threads · $(awk '/MemTotal/{printf "%.1f GB", $2/1048576}' /proc/meminfo)"
log "== graphics chip (Vulkan)"
vulkaninfo --summary 2>&1 | grep -E "deviceName|driverName|driverInfo|apiVersion|deviceType" | head -12 | tee -a "$OUT"
BENCH=$(command -v llama-bench || ls /app/llama-bench 2>/dev/null || find / -name llama-bench -type f 2>/dev/null | head -1)
log "== bench tool: $BENCH"
get() {
  [ -s "/data/models/$1" ] && return 0
  log "downloading $1"
  curl -sSL --retry 3 -o "/data/models/$1.part" "$2" && mv "/data/models/$1.part" "/data/models/$1"
  log "  $(du -h "/data/models/$1" | cut -f1)"
}
get qwen2.5-3b-q4.gguf "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf"
get qwen2.5-7b-q4.gguf "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
# -p 1500: reading a home's worth of context (the request + what the home has); -n 64: a spoken reply's length.
for m in qwen2.5-3b-q4 qwen2.5-7b-q4; do
  log ""; log "== $m — graphics chip"
  "$BENCH" -m "/data/models/$m.gguf" -ngl 99 -p 1500 -n 64 -r 2 2>&1 | grep -vE "^load_tensors|^llama_model_loader|^print_info" | tail -12 | tee -a "$OUT"
  log ""; log "== $m — processor only"
  "$BENCH" -m "/data/models/$m.gguf" -ngl 0 -p 1500 -n 64 -r 1 2>&1 | grep -vE "^load_tensors|^llama_model_loader|^print_info" | tail -8 | tee -a "$OUT"
done
log ""; log "DONE"
exec sleep infinity
