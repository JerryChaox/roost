#!/usr/bin/env bash
# End-to-end check of roost M1 on E2B Cloud: builds the sandbox directory,
# starts roost serve on a temporary SQLite database with a generated tenant
# key, creates a workspace, waits for it to be active, talks to Pi through
# the conversation API, and finally removes every sandbox it created (all
# carry the run's tenant label) and confirms none is left.
#
# Secrets are never printed. E2B_API_KEY and ANTHROPIC_API_KEY come from the
# environment or the repository's .env; they reach roost serve only as
# ROOST_SECRET_* environment variables, and E2B API calls read the key from a
# private header file, not from argv.
#
# The model is configurable, for example to go through an LLM gateway:
#   ANTHROPIC_BASE_URL     the Anthropic API's base URL (default: Anthropic's)
#   ROOST_E2E_MODEL        <provider>/<model id> (default: anthropic/claude-haiku-4-5)
#   ROOST_E2E_MODEL_INFO   model metadata for a model outside Pi's catalog, as a
#                          YAML flow mapping, e.g. '{ context_window: 1000000,
#                          max_output_tokens: 32768, reasoning: true, input: [text] }'
#
# Usage: scripts/e2e-m1.sh [--failures]
#   --failures  also kill the driver (a new grant must take over on the same
#               sandbox, and the old driver token must get 401) and kill the
#               agent host mid-run (the run must still complete).
set -euo pipefail

failures=0
for arg in "$@"; do
  case "$arg" in
    --failures) failures=1 ;;
    *) echo "usage: $0 [--failures]" >&2; exit 2 ;;
  esac
done

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/roost-e2e.XXXXXX")"
chmod 700 "$work"
port="${ROOST_E2E_PORT:-17070}"
api="http://127.0.0.1:$port"
tenant="e2e-$(openssl rand -hex 4)"
ws="ws-1"
log="$work/serve.log"
serve_pid=""

say() { printf '[e2e %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# --- secrets, kept in files under $work (mode 600), never echoed
set -a
# shellcheck disable=SC1091
[ -f "$root/.env" ] && . "$root/.env"
set +a
: "${E2B_API_KEY:?set E2B_API_KEY in the environment or .env}"
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY in the environment or .env}"
model="${ROOST_E2E_MODEL:-anthropic/claude-haiku-4-5}"
model_info="${ROOST_E2E_MODEL_INFO:-}"
base_url_line=""
[ -n "${ANTHROPIC_BASE_URL:-}" ] && base_url_line="base_url: $ANTHROPIC_BASE_URL, "
model_info_line=""
[ -n "$model_info" ] && model_info_line="  model_info: $model_info"
umask 077
printf 'X-API-Key: %s\n' "$E2B_API_KEY" >"$work/e2b.hdr"
tenant_key="$(openssl rand -hex 24)"
printf 'Authorization: Bearer %s\n' "$tenant_key" >"$work/tenant.hdr"
umask 022

e2b() { curl -sS -H @"$work/e2b.hdr" "$@"; }
call() { # METHOD PATH [BODY] → prints "<status> <body>"
  local m="$1" p="$2" b="${3:-}"
  if [ -n "$b" ]; then
    curl -sS -o "$work/out" -w '%{http_code}' -X "$m" -H @"$work/tenant.hdr" -H 'Content-Type: application/json' --data "$b" "$api$p"
  else
    curl -sS -o "$work/out" -w '%{http_code}' -X "$m" -H @"$work/tenant.hdr" "$api$p"
  fi
  printf ' %s\n' "$(cat "$work/out")"
}

labelled() { # ids of every running or paused sandbox of this run
  e2b -G "https://api.e2b.app/v2/sandboxes" --data-urlencode "metadata=roost.tenant=$tenant" \
    --data-urlencode "state=running,paused" --data-urlencode "limit=100" | jq -r '.[].sandboxID'
}

cleanup() {
  local rc=$?
  set +e
  if [ -n "$serve_pid" ] && kill -0 "$serve_pid" 2>/dev/null; then
    kill "$serve_pid"; wait "$serve_pid" 2>/dev/null
  fi
  say "cleanup: removing sandboxes labelled roost.tenant=$tenant"
  for id in $(labelled); do
    say "  kill $id: HTTP $(e2b -o /dev/null -w '%{http_code}' -X DELETE "https://api.e2b.app/sandboxes/$id")"
  done
  sleep 2
  left="$(labelled | wc -l | tr -d ' ')"
  say "cleanup: $left sandbox(es) left with the run's label"
  say "serve log: $log"
  rm -f "$work/e2b.hdr" "$work/tenant.hdr"
  exit "$rc"
}
trap cleanup EXIT

# exec_in SANDBOX USER ARGV...: runs a command in the sandbox through the
# provider's Exec (the key goes through the environment, not argv).
exec_in() { ( set -a; . "$root/.env"; set +a; "$work/e2e-exec" "$@" ); }

db() { sqlite3 -batch "$work/roost.db" "$@"; }

# stream_run CONV RUN [ON_TOOL_CALL]: follows the conversation's events until
# RUN ends, reconnecting after the last entry seen when the stream breaks (a
# driver or host restart cuts it). ON_TOOL_CALL, if given, runs in the
# background once, when the run's first assistant entry with a tool call
# arrives. Prints the run's final status.
stream_run() {
  local conv="$1" run="$2" hook="${3:-}" last=0 st="" ev="" data="" hooked=0 deadline=$((SECONDS + 300))
  while [ -z "$st" ] && [ $SECONDS -lt $deadline ]; do
    while IFS= read -r line; do
      case "$line" in
        "event: "*) ev="${line#event: }" ;;
        "id: "*) last="${line#id: }" ;;
        "data: "*)
          data="${line#data: }"
          case "$ev" in
            run)
              say "  run: $data"
              if [ "$(jq -r .run <<<"$data")" = "$run" ]; then
                case "$(jq -r .status <<<"$data")" in completed|failed|aborted) st="$(jq -r .status <<<"$data")"; break ;; esac
              fi ;;
            entry)
              say "  entry: $(jq -c '{cursor, kind, run}' <<<"$data")"
              if [ -n "$hook" ] && [ "$hooked" = 0 ] && [ "$(jq -r '.kind + " " + .run' <<<"$data")" = "assistant $run" ] \
                 && jq -e '[.raw | .. | objects | select(.type? == "toolCall")] | length > 0' <<<"$data" >/dev/null; then
                hooked=1; ( $hook ) >&2 &
              fi ;;
          esac ;;
      esac
    done < <(curl -sS -N --max-time 240 -H @"$work/tenant.hdr" "$api/v1/workspaces/$ws/conversations/$conv/events?after=$last" 2>/dev/null || true)
    if [ -z "$st" ]; then
      # The run may have ended while the stream was down: ask the listing.
      if [ "$(call GET "/v1/workspaces/$ws/conversations?key=e2e:thread-1" | cut -d' ' -f2- | jq -r '.conversations[0].active // empty' 2>/dev/null)" = false ] \
         && [ "$hooked" = 1 -o -z "$hook" ] && [ "$last" != 0 ]; then
        st="ended while the stream was down (conversation no longer active)"
      else
        say "  stream ended; reconnecting after cursor $last"; sleep 2
      fi
    fi
  done
  wait 2>/dev/null || true
  printf '%s' "${st:-timeout}"
}

# --- build
say "building the sandbox directory and roost"
"$root/scripts/build-sandbox.sh" "$work/sandbox"
(cd "$root" && go build -o "$work/roost" ./cmd/roost && go build -o "$work/e2e-exec" ./scripts/e2e-exec)

cat >"$work/roost.yaml" <<YAML
server: { listen: 127.0.0.1:$port, database: sqlite://./roost.db }
workspace:
  provider: { api_url: https://api.e2b.app, api_key: secret://e2b }
agent:
  model: $model
  thinking: low
$model_info_line
  system_prompt: { base: pi }
models:
  access: e2b
  providers:
    anthropic: { ${base_url_line}key: secret://anthropic }
tenants:
  $tenant: { api_keys: [secret://tenant-key] }
YAML

# --- serve
say "starting roost serve (tenant $tenant) on $api"
ROOST_SECRET_E2B="$E2B_API_KEY" ROOST_SECRET_ANTHROPIC="$ANTHROPIC_API_KEY" ROOST_SECRET_TENANT_KEY="$tenant_key" \
  env -u E2B_API_KEY -u ANTHROPIC_API_KEY "$work/roost" serve --config "$work/roost.yaml" --sandbox-dir "$work/sandbox" >"$log" 2>&1 &
serve_pid=$!
unset E2B_API_KEY ANTHROPIC_API_KEY
for _ in $(seq 50); do
  curl -s -o /dev/null "$api/v1/workspaces" && break
  kill -0 "$serve_pid" 2>/dev/null || { cat "$log" >&2; say "roost serve exited"; exit 1; }
  sleep 0.2
done

# --- workspace
say "PUT /v1/workspaces/$ws → $(call PUT "/v1/workspaces/$ws" '{"owner":"e2e-user"}')"
say "waiting for $ws to be active (a template build takes minutes the first time)"
deadline=$((SECONDS + 1500))
phase=""
while [ $SECONDS -lt $deadline ]; do
  out="$(call GET "/v1/workspaces/$ws")"
  p="$(printf '%s' "${out#* }" | jq -r .phase)"
  if [ "$p" != "$phase" ]; then say "  phase $p: ${out#* }"; phase="$p"; fi
  [ "$p" = active ] || [ "$p" = failed ] && break
  kill -0 "$serve_pid" 2>/dev/null || { say "roost serve exited"; exit 1; }
  sleep 3
done
if [ "$phase" != active ]; then
  say "workspace did not become active; last log lines:"; tail -n 30 "$log" >&2; exit 1
fi

# --- conversation
out="$(call POST "/v1/workspaces/$ws/conversations" '{"key":"e2e:thread-1"}')"
say "POST conversations → $out"
conv="$(printf '%s' "${out#* }" | jq -r .conversation)"
[ -n "$conv" ] && [ "$conv" != null ] || exit 1
out="$(call POST "/v1/workspaces/$ws/conversations" '{"key":"e2e:thread-1"}')"
say "POST conversations (same key) → $out"
say "GET conversations?key= → $(call GET "/v1/workspaces/$ws/conversations?key=e2e:thread-1")"

msg='{"id":"m-1","text":"Reply with exactly the word: pong","delivery":"queue"}'
say "POST messages → $(call POST "/v1/workspaces/$ws/conversations/$conv/messages" "$msg")"

status="$(stream_run "$conv" m-1)"
say "run m-1 ended: $status"

say "POST messages again (same id) → $(call POST "/v1/workspaces/$ws/conversations/$conv/messages" "$msg")"

out="$(call GET "/v1/workspaces/$ws/conversations/$conv/entries")"
say "transcript kinds: $(printf '%s' "${out#* }" | jq -c '[.entries[].kind]')"
say "assistant text: $(printf '%s' "${out#* }" | jq -c '[.entries[] | select(.kind=="assistant") | .raw] | last' | cut -c1-400)"
[ "$status" = completed ]

[ "$failures" = 1 ] || exit 0

# --- failure 1: driver loss
grant_row() { db "SELECT id || ' sandbox=' || sandbox_id || ' end_reason=' || coalesce(end_reason, '-') FROM grants ORDER BY issued_at"; }
old="$(db "SELECT id FROM grants WHERE ended_at IS NULL")"
sbx="$(db "SELECT sandbox_id FROM grants WHERE id = '$old'")"
( umask 077; db "SELECT 'Authorization: Bearer ' || driver_token FROM grants WHERE id = '$old'" >"$work/old.hdr" )
say "failure 1: kill roost-driver in $sbx (live grant $old)"
exec_in "$sbx" root pkill -KILL -x roost-driver && say "  pkill: ok"
deadline=$((SECONDS + 240))
until [ "$(db "SELECT count(*) FROM grants WHERE id = '$old' AND end_reason = 'driver_lost'")" = 1 ] \
      && [ "$(call GET "/v1/workspaces/$ws/conversations" | cut -d' ' -f1)" = 200 ]; do
  [ $SECONDS -lt $deadline ] || { say "no new grant took over"; grant_row >&2; tail -n 20 "$log" >&2; exit 1; }
  sleep 3
done
new="$(db "SELECT id FROM grants WHERE ended_at IS NULL")"
say "  new grant $new on $(db "SELECT sandbox_id FROM grants WHERE id = '$new'") (same sandbox: $([ "$(db "SELECT sandbox_id FROM grants WHERE id = '$new'")" = "$sbx" ] && echo yes || echo NO))"
say "  workspace: $(call GET "/v1/workspaces/$ws" | cut -d' ' -f2- | jq -c '{phase}')"
say "  transcript kept: $(call GET "/v1/workspaces/$ws/conversations/$conv/entries" | cut -d' ' -f2- | jq -c '[.entries[].kind]')"
code="$(curl -sS -o "$work/out" -w '%{http_code}' -H @"$work/old.hdr" -H 'Roost-Protocol: 1' "https://7070-$sbx.e2b.app/v1/health")"
rm -f "$work/old.hdr"
say "  old driver token on the driver endpoint → $code $(cat "$work/out")"
[ "$code" = 401 ] || exit 1
say "POST messages m-2 → $(call POST "/v1/workspaces/$ws/conversations/$conv/messages" '{"id":"m-2","text":"Reply with exactly the word: again"}')"
st2="$(stream_run "$conv" m-2)"
say "run m-2 ended: $st2"
[ "$st2" = completed ] || exit 1

# --- failure 2: agent host killed mid-run
kill_host() {
  sleep 4
  say "  >>> killing the agent host in $sbx (mid-tool)"
  # Anchored on the host's own command line: the driver's argv also names
  # main.js (inside --host-cmd), and must survive this.
  exec_in "$sbx" root pkill -KILL -f '^node .*[/]opt/roost/agent-pi/dist/main.js' && say "  >>> pkill: ok"
}
say "failure 2: a slow tool, and the agent host killed while it runs"
before="$(db "SELECT id FROM grants WHERE ended_at IS NULL")"
say "POST messages m-3 → $(call POST "/v1/workspaces/$ws/conversations/$conv/messages" \
  '{"id":"m-3","text":"Use the bash tool to run exactly this command: sleep 20 && echo done. Then reply with one short sentence saying what the command printed, or that it was interrupted."}')"
st3="$(stream_run "$conv" m-3 kill_host)"
say "run m-3 ended: $st3"
out="$(call GET "/v1/workspaces/$ws/conversations/$conv/entries?limit=200" | cut -d' ' -f2-)"
say "m-3 entries: $(jq -c '[.entries[] | select(.run == "m-3") | .kind]' <<<"$out")"
say "m-3 tool results: $(jq -c '[.entries[] | select(.run == "m-3" and .kind == "tool_result") | .raw | tostring | .[0:300]]' <<<"$out")"
say "m-3 final answer: $(jq -c '[.entries[] | select(.run == "m-3" and .kind == "assistant")] | last | .raw | [.. | objects | select(.type? == "text") | .text] ' <<<"$out")"
say "grants:"; grant_row | sed 's/^/    /' >&2
after="$(db "SELECT id FROM grants WHERE ended_at IS NULL")"
say "live grant across the host crash: $before → $after ($([ "$before" = "$after" ] && echo 'unchanged: the driver restarted its host' || echo 'CHANGED: the driver was lost too'))"
[ "$before" = "$after" ] || exit 1
case "$st3" in completed|ended*) ;; *) exit 1 ;; esac
