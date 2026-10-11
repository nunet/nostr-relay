#!/usr/bin/env bash
# gated-relay, driven by a real Nostr client (nak) over a real websocket.
#
#   tests/gated-relay.sh
#
# Needs: nak, curl, jq, and a python with the relay's two dependencies
# (RELAY_PYTHON, default python3; see relay/requirements.txt). If MAILBOX is set to
# a nostr-mailbox `mailbox` script, it also runs the real mailbox end to end.
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${RELAY_PYTHON:-python3}"
PORT="${RELAY_TEST_PORT:-17448}"
URL="ws://127.0.0.1:$PORT"
T="$(mktemp -d)"
export HOME="$T/home"; mkdir -p "$HOME"   # nak keeps state in ~/.config/nak: never share the caller's
cleanup() { [[ -n "${RPID:-}" ]] && kill "$RPID" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

key() { printf '%064x' "$1"; }
KA=$(key 1); KB=$(key 2); KC=$(key 3); KX=$(key 9)       # a, b, c members; x an outsider
PA=$(nak key public "$KA"); PB=$(nak key public "$KB"); PC=$(nak key public "$KC"); PX=$(nak key public "$KX")

write_conf() { # <relay_urls toml list> <member hex...>
    local urls="$1"; shift
    { printf '[network]\naddress = "127.0.0.1"\nport = %s\n[gate]\nrelay_urls = %s\n' "$PORT" "$urls"
      printf '[authorization]\npubkey_whitelist = [\n'; printf '  "%s",\n' "$@"; printf ']\n'; } > "$T/config.toml.new"
    mv "$T/config.toml.new" "$T/config.toml"
}
write_conf "[\"$URL\"]" "$PA" "$PB" "$PC"

RELAY_CONFIG="$T/config.toml" RELAY_DB="$T/db/relay.sqlite3" "$PY" -I "$HERE/relay/gated_relay.py" > "$T/relay.log" 2>&1 &
RPID=$!
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 0.1; done
# Up AND ours: a stale relay already on the port would answer the health check while
# this one died on bind -- and every check below would test the wrong process.
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && kill -0 "$RPID" 2>/dev/null \
    || { echo "relay did not start (port $PORT taken?):"; cat "$T/relay.log"; exit 1; }

as()  { local k="$1"; shift; NOSTR_SECRET_KEY="$k" timeout 15 nak "$@" 2>&1; }
pub() { as "$1" event --auth "${@:2}" "$URL"; }           # <key> <event args...>
req() { as "$1" req --auth "${@:2}" "$URL" | grep '^{' ; }  # <key> <filter args...>

fails=0
check() { if (( $2 )); then printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); else printf '  ok    %s\n' "$1"; fi; }
echo ""

# --- the HTTP side ------------------------------------------------------------
info="$(curl -sf -H 'Accept: application/nostr+json' "http://127.0.0.1:$PORT/")"
check "NIP-11 says auth is required"            "$([[ "$(jq -r .limitation.auth_required <<<"$info")" == true ]]; echo $?)"

# --- publishing ---------------------------------------------------------------
out="$(as "$KA" event -k 3838 -c nope "$URL")"
check "no sign-in: publishing refused"          "$(grep -q 'auth-required' <<<"$out"; echo $?)"
out="$(pub "$KX" -k 3838 -c intruder --tag "p=$PB")"
check "an outsider cannot publish"              "$(! grep -q 'success' <<<"$out"; echo $?)"
out="$(pub "$KA" -k 3838 -c "a to b" --tag "p=$PB")"
check "a member publishes, signed in"           "$(grep -q 'success' <<<"$out"; echo $?)"
ID_AB="$(grep '^{' <<<"$out" | jq -r .id)"
pub "$KC" -k 3838 -c "c to a" --tag "p=$PA" >/dev/null
# A member relaying someone else's (validly signed) event: refused.
ev_b="$(NOSTR_SECRET_KEY="$KB" nak event -k 3838 -c "b's own" --tag "p=$PC" 2>/dev/null)"
out="$(echo "$ev_b" | NOSTR_SECRET_KEY="$KA" timeout 15 nak event --auth "$URL" 2>&1)"
check "a member cannot publish another's event" "$(! grep -q 'success' <<<"$out"; echo $?)"

# --- reading: only what you wrote, or what names you ---------------------------
check "the addressee reads it"                  "$(req "$KB" -k 3838 --tag "p=$PB" | jq -r .content | grep -qx 'a to b'; echo $?)"
check "the author reads it, by id"              "$([[ "$(req "$KA" -i "$ID_AB" | jq -r .id)" == "$ID_AB" ]]; echo $?)"
check "a member not named reads nothing"        "$([[ -z "$(req "$KC" -k 3838 --tag "p=$PB")" ]]; echo $?)"
check "an unfiltered read is still scoped"      "$([[ "$(req "$KC" -k 3838 | jq -r .content | sort | tr '\n' '|')" == "c to a|" ]]; echo $?)"
check "an outsider reads nothing"               "$([[ -z "$(req "$KX" -k 3838)" ]]; echo $?)"
out="$(as "$KX" req -k 3838 "$URL")"
check "no sign-in: the read is CLOSED, not empty-handed silently" "$([[ -z "$(grep '^{' <<<"$out")" ]]; echo $?)"

# --- deletion (the mailbox's unsend) ---------------------------------------------
pub "$KC" -k 5 --tag "e=$ID_AB" >/dev/null
check "a non-author's deletion is ignored"      "$([[ -n "$(req "$KB" -i "$ID_AB")" ]]; echo $?)"
pub "$KA" -k 5 --tag "e=$ID_AB" >/dev/null
check "the author's deletion removes it"        "$([[ -z "$(req "$KB" -i "$ID_AB")" ]]; echo $?)"

# --- the allowlist reloads without a restart --------------------------------------
sleep 1; write_conf "[\"$URL\"]" "$PA" "$PB" "$PC" "$PX"
check "an added key is accepted, no restart"    "$(pub "$KX" -k 3838 -c joined --tag "p=$PA" | grep -q success; echo $?)"
sleep 1; write_conf "[\"$URL\"]" "$PA" "$PB"
check "a revoked key is refused, no restart"    "$(! pub "$KC" -k 3838 -c gone --tag "p=$PA" | grep -q success; echo $?)"
check "the relay is the same process"           "$(kill -0 "$RPID"; echo $?)"

# --- AUTH must name this relay ------------------------------------------------------
sleep 1; write_conf '["wss://relay.example.org"]' "$PA" "$PB"
check "AUTH naming another relay is refused"    "$(! pub "$KA" -k 3838 -c wrongurl --tag "p=$PB" | grep -q success; echo $?)"
sleep 1; write_conf "[\"$URL\"]" "$PA" "$PB"

# --- the real mailbox, end to end -------------------------------------------------
if [[ -n "${MAILBOX:-}" ]]; then
    for n in a b; do
        mkdir -p "$T/$n/tools/nostr-mailbox" "$T/$n/harness"
        echo "AVATAR_NAME=$n" > "$T/$n/harness/config"
        jq -n --arg a "$(nak encode npub "$PA")" --arg ah "$PA" --arg b "$(nak encode npub "$PB")" --arg bh "$PB" \
            '{a:{npub:$a,pubkey_hex:$ah,display:"a"}, b:{npub:$b,pubkey_hex:$bh,display:"b"}}' \
            > "$T/$n/tools/nostr-mailbox/registry.json"
    done
    mb() { local n="$1" k="$2"; shift 2; AVATAR_ROOT="$T/$n" NOSTR_MAILBOX_NSEC="$k" NOSTR_MAILBOX_RELAY="$URL" "$MAILBOX" "$@" 2>&1; }
    out="$(mb a "$KA" send b --subject "hello over the gate" "the body")"
    check "mailbox: a sends to b"               "$(grep -qiE 'sent|published' <<<"$out"; echo $?)"
    out="$(mb b "$KB" fetch)"
    check "mailbox: b fetches it"               "$(grep -q 'claimed 1 new' <<<"$out"; echo $?)"
    check "mailbox: b reads the body"           "$(mb b "$KB" list | grep -q 'hello over the gate'; echo $?)"
    (( fails )) && sed 's/^/        | /' <<<"$out"
fi

echo ""
if (( fails )); then echo "  --- relay log ---"; sed 's/^/  | /' "$T/relay.log"; echo "  $fails check(s) failed"; exit 1; fi
echo "  all checks passed"
