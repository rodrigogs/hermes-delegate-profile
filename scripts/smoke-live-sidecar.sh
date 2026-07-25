#!/usr/bin/env bash
# Live smoke test for the Capability Router sidecar — run ON the box.
#
# The pytest suite proves behaviour against fixtures; this proves the SERVICE:
# the deployed console is served, the token gate holds, the write path refuses a
# drifted plan, compaction demands its confirm phrase, the live router.yaml is
# never modified by a dry run, and a real route() lands a replayable trace that
# comes back through the authenticated /routes endpoint.
#
#   ssh <box> 'bash /path/to/smoke-live-sidecar.sh'
#
# Exits non-zero on the first failed assertion count, so it is CI-usable.
set -u
P=/home/rodrigo/.hermes/plugins/delegate-profile
TOK=$(cat /home/rodrigo/.hermes/profiles/rodrigo/webui/sidecar-auth/capability-router.token)
H="X-Hermes-Sidecar-Token: $TOK"
B=http://127.0.0.1:8791
pass=0; fail=0
chk(){ if [ "$2" = "$3" ]; then echo "  PASS  $1 ($3)"; pass=$((pass+1)); else echo "  FAIL  $1 expected=$3 got=$2"; fail=$((fail+1)); fi; }

echo "=== LIVE sidecar smoke (production policy, real service) ==="
# restart sidecar so it serves the freshly synced code
systemctl --user restart hermes-router-sidecar.service; sleep 3
chk "sidecar active" "$(systemctl --user is-active hermes-router-sidecar.service)" "active"
chk "/health 200" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 $B/health)" "200"
chk "/console 200 (served)" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 $B/console)" "200"
chk "/console is the command-deck" "$(curl -s --max-time 5 $B/console | grep -c 'class=\"rail\"')" "1"
chk "/status needs token" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 $B/status)" "401"
chk "/status 200 w/ token" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 -H "$H" $B/status)" "200"
chk "/liveness 200" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 -H "$H" $B/liveness)" "200"
chk "/routes 200" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 -H "$H" $B/routes)" "200"
chk "/routes unknown id 404" "$(curl -s -o /dev/null -w %{http_code} --max-time 5 -H "$H" "$B/routes?id=nope")" "404"

echo "--- write path against the LIVE policy (dry run only, then a hash-drift refusal) ---"
MD5_BEFORE=$(md5sum $P/router.yaml | cut -d' ' -f1)
PLAN=$(curl -s --max-time 8 -X POST -H "$H" -H "Content-Type: application/json" -d '{"policy":{"enabled":true}}' $B/plan)
chk "plan valid on live policy" "$(echo "$PLAN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["valid"])')" "True"
chk "plan produced a base_hash" "$(echo "$PLAN" | python3 -c 'import sys,json;print(bool(json.load(sys.stdin)["base_hash"]))')" "True"
chk "stale apply refused 409" "$(curl -s -o /dev/null -w %{http_code} --max-time 8 -X POST -H "$H" -H "Content-Type: application/json" -d '{"plan":{"base_hash":"deadbeef"},"policy":{"enabled":true}}' $B/apply)" "409"
chk "compaction needs confirm 400" "$(curl -s -o /dev/null -w %{http_code} --max-time 8 -X POST -H "$H" -H "Content-Type: application/json" -d '{"action":"compaction"}' $B/apply)" "400"
chk "LIVE router.yaml untouched" "$(md5sum $P/router.yaml | cut -d' ' -f1)" "$MD5_BEFORE"

echo "--- replay chain on the live service ---"
cd "$P"
/usr/local/lib/hermes-agent/venv/bin/python3 -c "
import sys; sys.path.insert(0,'.')
from router.adapter import route
from router.durable_decision_log import DurableDecisionLog
import yaml
cfg=yaml.safe_load(open('router.yaml'))
r=route(task='Debug a race condition in the live smoke test', config=cfg, decision_log=DurableDecisionLog())
print('routed_model='+str(r.get('model')))
" 2>&1 | tail -1
N=$(curl -s --max-time 5 -H "$H" $B/routes | python3 -c 'import sys,json;print(json.load(sys.stdin)["count"])')
chk "trace readable through sidecar (count>0)" "$([ "$N" -gt 0 ] && echo yes || echo no)" "yes"
ID=$(curl -s --max-time 5 -H "$H" $B/routes | python3 -c 'import sys,json;print(json.load(sys.stdin)["routes"][0]["id"])')
STAGES=$(curl -s --max-time 5 -H "$H" "$B/routes?id=$ID" | python3 -c 'import sys,json;print(",".join(s["stage"] for s in json.load(sys.stdin).get("steps",[])))')
chk "replay steps present" "$([ -n "$STAGES" ] && echo yes || echo no)" "yes"
echo "  steps: $STAGES"

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
