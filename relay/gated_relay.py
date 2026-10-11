#!/usr/bin/env python3
"""gated-relay -- a closed Nostr relay for the avatar mailbox fabric.

Reachable from the public internet and still private:

  * every connection is challenged at once (NIP-42), and nothing -- no EVENT, no
    REQ -- is served until it signs in with a key on the allowlist;
  * a signed-in key may publish only its OWN events;
  * a signed-in key may read only events it wrote or that are addressed to it (a
    `p` tag naming it). A mailbox needs nothing else, and it means even a member
    -- a short-lived worker clone, say -- cannot watch who else mails whom.

Why not nostr-rs-relay, which this replaces: its allowlist gates publishing only,
and its NIP-42 option protects only the DM kinds (4/44/1059). The mailbox uses
kind 3838, so on a public address anyone could have read the fabric's metadata.

The allowlist is the same `[authorization] pubkey_whitelist` in config.toml that
scripts/sync-allowlist writes, and it is RELOADED when the file changes -- adding or
revoking a key needs no restart. Also read from config.toml:

  [network]  address, port
  [limits]   max_event_bytes, max_ws_message_bytes
  [options]  reject_future_seconds
  [gate]     relay_urls -- the URLs clients may name in their AUTH event (NIP-42's
             `relay` tag). Empty means any; set it on a public deployment.

Storage is SQLite. Supported: NIP-01, NIP-09 (deletion, by the author only), NIP-11
(the info document), NIP-42. Ephemeral kinds (20000-29999) are relayed, not stored.
Replaceable kinds are stored like any other -- nothing on the fabric uses them.
"""
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
import tomllib
from http import HTTPStatus

from coincurve import PublicKeyXOnly
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

CONF_PATH = os.environ.get("RELAY_CONFIG", "/usr/src/app/config.toml")
DB_PATH = os.environ.get("RELAY_DB", "/usr/src/app/db/gated-relay.sqlite3")
AUTH_KIND = 22242
AUTH_WINDOW = 600            # seconds an AUTH event's created_at may be off by
MAX_SUBS = 20                # per connection
MAX_FILTERS = 10             # per REQ
MAX_LIMIT = 5000             # per filter

log = logging.getLogger("gated-relay")


# ------------------------------------------------------------------ config ---
class Config:
    """config.toml, re-read whenever its mtime changes."""

    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.load()

    def load(self):
        with open(self.path, "rb") as f:
            raw = tomllib.load(f)
        self.mtime = os.stat(self.path).st_mtime
        net, lim, opt = raw.get("network", {}), raw.get("limits", {}), raw.get("options", {})
        self.address = net.get("address", "0.0.0.0")
        self.port = int(net.get("port", 8080))
        self.max_event_bytes = int(lim.get("max_event_bytes", 131072))
        self.max_ws_bytes = int(lim.get("max_ws_message_bytes", 262144))
        self.reject_future = int(opt.get("reject_future_seconds", 1800))
        self.allow = {k.lower() for k in raw.get("authorization", {}).get("pubkey_whitelist", [])}
        self.relay_urls = {norm_url(u) for u in raw.get("gate", {}).get("relay_urls", [])}
        self.info = raw.get("info", {})

    def fresh(self):
        """The current allowlist, reloading the file if it changed. A broken edit
        keeps the last good list rather than locking everyone out (or in)."""
        try:
            if os.stat(self.path).st_mtime != self.mtime:
                self.load()
                log.info("config reloaded: %d key(s) on the allowlist", len(self.allow))
        except Exception as e:  # noqa: BLE001 -- keep serving on a bad edit
            log.error("config reload failed, keeping the previous one: %s", e)
        return self


def norm_url(u):
    u = u.strip().lower().rstrip("/")
    return u


# ------------------------------------------------------------------ events ---
def event_id(ev):
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                     separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(ser.encode()).hexdigest()


def is_hex(s, n):
    return isinstance(s, str) and len(s) == n and all(c in "0123456789abcdef" for c in s)


def check_event(ev):
    """None if the event is well-formed, correctly hashed and signed; else why not."""
    if not isinstance(ev, dict):
        return "invalid: not an object"
    if not (is_hex(ev.get("id"), 64) and is_hex(ev.get("pubkey"), 64) and is_hex(ev.get("sig"), 128)):
        return "invalid: id, pubkey or sig malformed"
    if not isinstance(ev.get("created_at"), int) or not isinstance(ev.get("kind"), int):
        return "invalid: created_at or kind"
    if not isinstance(ev.get("content"), str) or not isinstance(ev.get("tags"), list) \
            or not all(isinstance(t, list) and all(isinstance(x, str) for x in t) for t in ev["tags"]):
        return "invalid: content or tags"
    if event_id(ev) != ev["id"]:
        return "invalid: id does not match the event"
    try:
        ok = PublicKeyXOnly(bytes.fromhex(ev["pubkey"])).verify(bytes.fromhex(ev["sig"]), bytes.fromhex(ev["id"]))
    except Exception:  # noqa: BLE001
        ok = False
    return None if ok else "invalid: bad signature"


def tag_values(ev, name):
    return [t[1] for t in ev["tags"] if len(t) >= 2 and t[0] == name]


def readable_by(ev, me):
    return ev["pubkey"] == me or me in tag_values(ev, "p")


def matches(ev, f):
    if "ids" in f and ev["id"] not in f["ids"]:
        return False
    if "authors" in f and ev["pubkey"] not in f["authors"]:
        return False
    if "kinds" in f and ev["kind"] not in f["kinds"]:
        return False
    if "since" in f and ev["created_at"] < f["since"]:
        return False
    if "until" in f and ev["created_at"] > f["until"]:
        return False
    for k, vals in f.items():
        if k.startswith("#") and len(k) == 2:
            if not set(tag_values(ev, k[1])) & set(vals):
                return False
    return True


# ----------------------------------------------------------------- storage ---
class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, pubkey TEXT NOT NULL,
                kind INTEGER NOT NULL, created_at INTEGER NOT NULL, json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tags (id TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS tags_kv ON tags (k, v);
            CREATE INDEX IF NOT EXISTS tags_id ON tags (id);
            CREATE INDEX IF NOT EXISTS events_pk ON events (pubkey, created_at);
            CREATE INDEX IF NOT EXISTS events_kind ON events (kind, created_at);
        """)

    def has(self, eid):
        return self.db.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone() is not None

    def put(self, ev):
        self.db.execute("BEGIN")
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                        (ev["id"], ev["pubkey"], ev["kind"], ev["created_at"], json.dumps(ev, ensure_ascii=False)))
        for t in ev["tags"]:
            if len(t) >= 2 and len(t[0]) == 1:
                self.db.execute("INSERT INTO tags VALUES (?,?,?)", (ev["id"], t[0], t[1]))
        self.db.execute("COMMIT")

    def delete_by(self, ids, author):
        """NIP-09: an event is deleted only by its own author."""
        n = 0
        for eid in ids:
            if self.db.execute("SELECT 1 FROM events WHERE id=? AND pubkey=?", (eid, author)).fetchone():
                self.db.execute("DELETE FROM events WHERE id=?", (eid,))
                self.db.execute("DELETE FROM tags WHERE id=?", (eid,))
                n += 1
        return n

    def query(self, f, me):
        """Events matching one filter that `me` may read, newest first. The read rule
        is in the SQL, not applied afterwards, so a limit counts readable events."""
        where, args = ["(e.pubkey = ? OR e.id IN (SELECT id FROM tags WHERE k='p' AND v=?))"], [me, me]
        for col, key in (("e.id", "ids"), ("e.pubkey", "authors"), ("e.kind", "kinds")):
            if key in f:
                vals = list(f[key])
                if not vals:
                    return []
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args += vals
        if "since" in f:
            where.append("e.created_at >= ?"); args.append(f["since"])
        if "until" in f:
            where.append("e.created_at <= ?"); args.append(f["until"])
        for k, vals in f.items():
            if k.startswith("#") and len(k) == 2:
                vals = list(vals)
                if not vals:
                    return []
                where.append(f"e.id IN (SELECT id FROM tags WHERE k=? AND v IN ({','.join('?' * len(vals))}))")
                args += [k[1]] + vals
        limit = min(int(f.get("limit", MAX_LIMIT)), MAX_LIMIT)
        sql = f"SELECT e.json FROM events e WHERE {' AND '.join(where)} ORDER BY e.created_at DESC, e.id LIMIT ?"
        return [json.loads(r[0]) for r in self.db.execute(sql, args + [limit])]


def valid_filter(f):
    if not isinstance(f, dict):
        return False
    for k, v in f.items():
        if k in ("ids", "authors"):
            if not isinstance(v, list) or not all(is_hex(x, 64) for x in v):
                return False
        elif k == "kinds":
            if not isinstance(v, list) or not all(isinstance(x, int) for x in v):
                return False
        elif k in ("since", "until", "limit"):
            if not isinstance(v, int):
                return False
        elif k.startswith("#") and len(k) == 2:
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return False
        else:
            return False
    return True


# ------------------------------------------------------------------- relay ---
class Relay:
    def __init__(self, conf, store):
        self.conf = conf
        self.store = store
        self.clients = set()

    async def handler(self, ws):
        c = Client(ws)
        self.clients.add(c)
        try:
            await c.send(["AUTH", c.challenge])
            async for raw in ws:
                await self.on_message(c, raw)
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(c)

    async def on_message(self, c, raw):
        try:
            msg = json.loads(raw)
            verb = msg[0]
        except Exception:  # noqa: BLE001
            return await c.send(["NOTICE", "invalid: not a JSON array"])
        if verb == "AUTH" and len(msg) == 2:
            return await self.on_auth(c, msg[1])
        if verb == "EVENT" and len(msg) == 2:
            return await self.on_event(c, msg[1])
        if verb == "REQ" and len(msg) >= 3 and isinstance(msg[1], str):
            return await self.on_req(c, msg[1], msg[2:])
        if verb == "CLOSE" and len(msg) == 2:
            c.subs.pop(msg[1], None)
            return None
        return await c.send(["NOTICE", f"invalid: unsupported message {verb!r}"])

    async def on_auth(self, c, ev):
        eid = ev.get("id", "") if isinstance(ev, dict) else ""
        why = check_event(ev)
        conf = self.conf.fresh()
        if why is None and ev["kind"] != AUTH_KIND:
            why = "invalid: not an AUTH event"
        if why is None and c.challenge not in tag_values(ev, "challenge"):
            why = "invalid: wrong challenge"
        if why is None and abs(ev["created_at"] - time.time()) > AUTH_WINDOW:
            why = "invalid: AUTH event too old or in the future"
        if why is None and conf.relay_urls and not {norm_url(u) for u in tag_values(ev, "relay")} & conf.relay_urls:
            why = "invalid: AUTH names a different relay"
        if why is None and ev["pubkey"] not in conf.allow:
            why = "restricted: this key is not on the allowlist"
        if why:
            log.info("auth refused (%s): %s", ev.get("pubkey", "?")[:12] if isinstance(ev, dict) else "?", why)
            return await c.send(["OK", eid, False, why])
        c.pubkey = ev["pubkey"]
        log.info("auth ok: %s", c.pubkey[:12])
        return await c.send(["OK", eid, True, ""])

    def signed_in(self, c):
        """The connection's key, if it signed in and is STILL on the allowlist --
        a revoked key loses access on its next message, not at its next connect."""
        return c.pubkey if c.pubkey and c.pubkey in self.conf.fresh().allow else None

    async def on_event(self, c, ev):
        eid = ev.get("id", "") if isinstance(ev, dict) else ""
        me = self.signed_in(c)
        if not me:
            return await c.send(["OK", eid, False, "auth-required: sign in with an allowlisted key"])
        if len(json.dumps(ev)) > self.conf.max_event_bytes:
            return await c.send(["OK", eid, False, "invalid: event too large"])
        why = check_event(ev)
        if why:
            return await c.send(["OK", eid, False, why])
        if ev["pubkey"] != me:
            return await c.send(["OK", eid, False, "restricted: you may publish only your own events"])
        if ev["kind"] == AUTH_KIND:
            return await c.send(["OK", eid, False, "invalid: AUTH events are not stored"])
        if ev["created_at"] > time.time() + self.conf.reject_future:
            return await c.send(["OK", eid, False, "invalid: created_at too far in the future"])
        if 20000 <= ev["kind"] < 30000:          # ephemeral: relay, never store
            await self.broadcast(ev)
            return await c.send(["OK", eid, True, ""])
        if self.store.has(eid):
            return await c.send(["OK", eid, True, "duplicate: already have it"])
        if ev["kind"] == 5:
            n = self.store.delete_by(tag_values(ev, "e"), me)
            log.info("deletion by %s: %d event(s) removed", me[:12], n)
        self.store.put(ev)
        await c.send(["OK", eid, True, ""])
        await self.broadcast(ev)

    async def on_req(self, c, sub, filters):
        me = self.signed_in(c)
        if not me:
            return await c.send(["CLOSED", sub, "auth-required: sign in with an allowlisted key"])
        if len(filters) > MAX_FILTERS or not all(valid_filter(f) for f in filters):
            return await c.send(["CLOSED", sub, "invalid: bad filter"])
        if sub not in c.subs and len(c.subs) >= MAX_SUBS:
            return await c.send(["CLOSED", sub, "invalid: too many subscriptions"])
        c.subs[sub] = filters
        seen = set()
        for f in filters:
            for ev in self.store.query(f, me):
                if ev["id"] not in seen:
                    seen.add(ev["id"])
                    await c.send(["EVENT", sub, ev])
        await c.send(["EOSE", sub])

    async def broadcast(self, ev):
        for c in list(self.clients):
            me = self.signed_in(c)
            if not me or not readable_by(ev, me):
                continue
            for sub, filters in list(c.subs.items()):
                if any(matches(ev, f) for f in filters):
                    await c.send(["EVENT", sub, ev])


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.challenge = secrets.token_hex(16)
        self.pubkey = None
        self.subs = {}

    async def send(self, msg):
        try:
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
        except ConnectionClosed:
            pass


# --------------------------------------------------------------- http side ---
def http_hook(conf):
    def process_request(connection, request):
        if request.path == "/health":
            return connection.respond(HTTPStatus.OK, "ok\n")
        if "application/nostr+json" in request.headers.get("Accept", ""):
            info = {
                "name": conf.info.get("name", "gated-relay"),
                "description": conf.info.get("description", "closed relay"),
                "supported_nips": [1, 9, 11, 42],
                "software": "nunet/applications/tools/nostr-relay gated-relay",
                "limitation": {"auth_required": True, "restricted_writes": True,
                               "max_message_length": conf.max_ws_bytes},
            }
            resp = connection.respond(HTTPStatus.OK, json.dumps(info))
            resp.headers["Content-Type"] = "application/nostr+json"
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
        return None
    return process_request


async def main():
    logging.basicConfig(level=os.environ.get("RELAY_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    conf = Config(CONF_PATH)
    if not conf.relay_urls:
        log.warning("[gate] relay_urls is empty: AUTH accepts any relay URL -- set it on a public deployment")
    relay = Relay(conf, Store(DB_PATH))
    log.info("listening on %s:%d, %d key(s) on the allowlist", conf.address, conf.port, len(conf.allow))
    async with serve(relay.handler, conf.address, conf.port, max_size=conf.max_ws_bytes,
                     process_request=http_hook(conf), ping_interval=30):
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
