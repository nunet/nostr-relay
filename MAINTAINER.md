# Maintainer runbook — nostr-mailbox provisioning

Agents never mint or hold private keys. The **operator/maintainer** mints one keypair per
avatar, stores the private key in `pass`, and publishes the public parts to
`registry.json`. avatar-builder wires the shared relay + network + injection. This file is
the command block to run **on the host** (not by an agent).

## One command

Everything below is wrapped in one script, so provisioning is never assembled from
snippets:

```sh
cd <your-path>/applications/tools/nostr-relay
scripts/provision undergryd-software-engineer avatar-builder          # steps 3-5
scripts/provision --mint undergryd-software-engineer avatar-builder   # steps 1 + 3-5
```

It builds the address book, writes the relay allowlist, and brings the relay and
the shared network up — reading every key from `pass`, so nothing is retyped or
read off a screen. `--mint` is opt-in because **minting replaces an avatar's
keys**, and an avatar already published then has to be republished.

Step 2 (the avatar's own secret declarations) is the one part the script does not
do: it edits an avatar repo, which is avatar-builder's side. Step 6 — starting
each avatar's mailbox — is deliberately separate too; see that section.

The sections below are what it runs, in order — read them to understand it, or to
do a step by hand.

## 1. Mint a keypair per avatar

`nostr-mailbox` ships a script that mints and stores in one step. It needs only **docker
and `pass`** on the host — no Go toolchain, no extra CLI:

```sh
cd <your-path>/applications/tools/nostr-mailbox
scripts/mint-avatar-keys undergryd-software-engineer avatar-builder
```

Per avatar it writes **four** `pass` entries — the whole identity lives in `pass`,
nothing about it is left on a screen:

| entry | what |
|---|---|
| `nunet/avatars/<avatar>/nostr-nsec` | the private key (64-char hex; the container also accepts `nsec1…`) |
| `nunet/avatars/<avatar>/nostr-pubkey-hex` | the public key → the relay `pubkey_whitelist` |
| `nunet/avatars/<avatar>/nostr-npub` | the public key, bech32 → `registry.json` |
| `nunet/avatars/<avatar>/mailbox-api-token` | the bearer token between that avatar's `mailbox` CLI and its own container |

The public parts are echoed as well, but **only for reassurance** — steps 3 and 4
read them back out of `pass`, so there is never a reason to copy one from
scrollback. **The private key is never printed**: it goes container stdout →
`pass` stdin, reaching neither screen, scrollback, nor shell history. `PASS_PREFIX`
(default `nunet/avatars`) overrides the store layout.

An avatar minted before the public halves were stored has only two entries;
re-running the script gives it all four — and a new keypair with them.

<details>
<summary>By hand instead, with <code>nak</code></summary>

```sh
# per avatar, e.g. AVATAR=undergryd-software-engineer
nak key generate                 # prints the private key (hex)
SK=<hex-from-above>
nak key public  "$SK"            # pubkey (hex)  -> registry pubkey_hex
nak encode nsec "$SK"            # nsec1...      -> store in pass
nak encode npub "$(nak key public "$SK")"   # npub1... -> registry npub

printf '%s' "$SK" | pass insert -m nunet/avatars/<avatar>/nostr-nsec
```

The secret is on screen this way, so clear the scrollback afterwards.
</details>

`did:key` from the same secp256k1 pubkey (multikey `zQ3s…`, `0x2701` prefix) — derive with
the project's DID tool, or leave `did:key` blank in v1 and fill at nuActor integration.

## 2. Declare the secrets — in two different places, on purpose

The avatar's two mailbox secrets are wired in **separately**, and the split is the
whole point.

**The API token** goes in that avatar's `harness/config` `SANDBOX_SECRETS`, so `launch`
resolves it from `pass` at start and injects it into the agent — the agent's `mailbox`
CLI needs it to talk to its own mailbox:

```sh
"MAILBOX_API_TOKEN=pass:nunet/avatars/<avatar>/mailbox-api-token"
```

It is **per avatar**, shared between that avatar's sandbox and that avatar's mailbox
container — not one fleet-wide value. It authorizes calls to a local HTTP API and
signs nothing.

**The signing key must NEVER appear in `SANDBOX_SECRETS`.** That list is injected into
the *agent's* container, so an entry there leaves the avatar's private key readable by
the LLM for an entire session — the exact opposite of the property this fabric exists
to provide. It is not hypothetical: it is what happened, and the key was confirmed
present in a running agent's environment on 2026-08-28.

Instead the key is named in the avatar's `tools/nostr-mailbox/config`, which
`mailboxctl` sources on the **host** and hands to the mailbox container alone:

```sh
: "${PASS_PREFIX:=nunet/avatars}"
: "${MAILBOX_NSEC_PASS_PATH:=$PASS_PREFIX/$MAILBOX_AVATAR/nostr-nsec}"
: "${MAILBOX_TOKEN_PASS_PATH:=$PASS_PREFIX/$MAILBOX_AVATAR/mailbox-api-token}"
```

Those are the defaults, so an avatar using the standard `pass` layout needs no edit at
all. Nothing in that file is a secret — they are paths *into* `pass`, not values out of
it.

| secret | declared in | reaches |
|---|---|---|
| `NOSTR_MAILBOX_NSEC` | `tools/nostr-mailbox/config` | the mailbox container **only** |
| `MAILBOX_API_TOKEN` | `harness/config` **and** the above | the agent **and** the mailbox |

## 3. Build the address book

`registry.json` maps name → identity, so a human can say "send this to
undergryd-software-engineer" and so an arriving message can be logged under a name rather
than a 64-character key. It is **not** a control surface: a message is trusted because its
signature verifies.

Generate it from the keys already in `pass` — no retyping, and the private halves are
never read:

```sh
cd <your-path>/applications/tools/nostr-mailbox
scripts/build-registry undergryd-software-engineer avatar-builder > registry.json
```

**Each avatar gets its own copy**, materialized with its `tools/nostr-mailbox` and
writable by it. There is no shared original mounted into every mailbox: avatars will not
always share a filesystem — with each other or with this relay — and an address book is
something its holder may extend. Keep your master copy wherever suits (beside this relay
is fine while both live on your machine) and re-copy it when the fleet gains a member.

A mailbox **refuses to start** unless its own entry exists and the injected key's derived
pubkey matches that entry (`identity.go`, `loadIdentity`). That is deliberate: a wrong or
swapped key fails loud instead of misdelivering silently.

## 4. Pin the pubkey in the relay allowlist

`scripts/sync-allowlist` writes `pubkey_whitelist` in `config.toml` from the keys in
`pass`, then you restart the relay (`docker compose restart` in this repo). Closed
set = no spam/discovery problem internally.

```sh
scripts/sync-allowlist undergryd-software-engineer avatar-builder
```

It rewrites that block in place and leaves the rest of `config.toml` alone, so it
is safe to re-run whenever the fleet gains a member. It refuses if an avatar has
no key in `pass` rather than writing a short list — a silently short allowlist
locks an avatar out with a confusing error at the relay.

Unlike the address book, this one **is** access control — it is what the relay enforces
before accepting an event, and it lives with the relay because the relay is what reads
it.

`config.toml` is read **only at relay start**, so a change here means a restart. To
check whether the running relay is actually carrying the current allowlist — rather
than assuming — `harness-core/fleet mailbox` prints an `in relay` column per avatar,
in a menu that is also how you act on it. It does steps 3, 4 and 6 across the whole
fleet at once, which is what you want when an identity changes: every avatar holds
its own copy of the address book, and missing one fails silently. It lives in
harness-core because it needs the fleet map; this repo keeps only what the relay
itself needs.

## 5. Bring up the shared relay (once)

```sh
cd <your-path>/applications/tools/nostr-relay
docker compose up -d
```

This creates the always-on relay + the shared `nostr-mailbox-net` network that every
avatar's mailbox joins.

## 6. Per-avatar mailbox

The mailbox is **its own compose project**, started by hand from the avatar — *not* part
of the avatar's stack:

```sh
cd <avatar>/tools/nostr-mailbox
./mailboxctl build     # explicit; `up` never builds
./mailboxctl up        # needs the avatar's network and the relay's to exist
./mailboxctl status
```

Its compose file is called `mailbox.compose.yml`, and that name is load-bearing: a
harness folds every `tools/*/docker-compose.yml` in an avatar into the **agent's** own
compose invocation, and under that name two of the mailbox's properties became boot
blockers for the agent — an `external` network (compose validates those before starting
anything, so a relay that was down failed the whole stack) and a Go build context (a
compile error became an avatar boot error). Mail is not on the critical path of an agent
starting up, so every dependency now points *from* the mailbox *to* what it needs and
never back. A mailbox that is broken, absent or mid-rebuild is invisible to the agent.

**Nothing in the avatar's copy needs editing.** `mailboxctl` derives `AVATAR_ROOT` from
its own location and `MAILBOX_AVATAR` from the avatar's `harness/config`, so the copy
stays byte-identical to the component and cannot fork at the edge. Install the `mailbox`
CLI skill in the avatar too.

The container runs as the **host user**: its image is built with `MAILBOX_UID`/`MAILBOX_GID`
build args (the harness's `HOST_UID`/`HOST_GID`) so the `inbox/`/`outbox/` binds are
writable and the `/state` volume is not root-owned.
