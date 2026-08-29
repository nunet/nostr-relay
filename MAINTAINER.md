# Maintainer runbook — nostr-mailbox provisioning

Agents never mint or hold private keys. The **operator/maintainer** mints one keypair per
avatar, stores the private key in `pass`, and publishes the public parts to
`registry.json`. avatar-builder wires the relay, the address book and the key injection. This file is
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

`nostr-mailbox` ships a script that mints and stores in one step. It needs `pass`, and
either **`nak` on the host or docker** — no Go toolchain either way. Without a host
`nak`, docker runs it straight from its module path at the same commit the harness
image builds, so the minting side and the running side cannot be different code:

```sh
cd <your-path>/applications/tools/nostr-mailbox
scripts/mint-avatar-keys undergryd-software-engineer avatar-builder
```

Per avatar it writes these `pass` entries — the whole identity lives in `pass`,
nothing about it is left on a screen:

| entry | what |
|---|---|
| `nunet/avatars/<avatar>/nostr-nsec` | the private key (64-char hex, or `nsec1…`) |
| `nunet/avatars/<avatar>/nostr-pubkey-hex` | the public key → the relay `pubkey_whitelist` |
| `nunet/avatars/<avatar>/nostr-npub` | the public key, bech32 → `registry.json` |

The public parts are echoed as well, but **only for reassurance** — steps 3 and 4
read them back out of `pass`, so there is never a reason to copy one from
scrollback. **The private key is never printed**: it stays in a shell variable and
goes to `pass` on stdin, reaching neither screen, scrollback, shell history, nor any
process's argv. `PASS_PREFIX` (default `nunet/avatars`) overrides the store layout.

An avatar minted before the public halves were stored has only the key itself;
re-running the script gives it all three — and a new keypair with them.

`mailbox-api-token`, a fourth entry older avatars may still carry, is dead: it
guarded a local HTTP API that no longer exists. Harmless to leave, safe to remove.

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

## 2. Wire the key into the avatar

One line in that avatar's `harness/config` `SANDBOX_SECRETS`, so `launch` resolves it
from `pass` at start and injects it into the agent:

```sh
"NOSTR_MAILBOX_NSEC=pass:nunet/avatars/<avatar>/nostr-nsec"
```

That is the only mailbox secret. `MAILBOX_API_TOKEN` no longer exists — it guarded a
local HTTP API that no longer exists either.

**Yes, this puts the signing key in the agent's environment, and yes, that is
deliberate.** An earlier design kept it in a separate container so the LLM could not
read it. That container signed whatever it was asked to sign, imposing no policy on
content or recipient, so it prevented *exfiltration* of the key and never *misuse* of
the identity — the agent could always ask it to send anything. It was not worth a Go
service, an image, a compose project and a boot-failure class that had already taken
an avatar offline.

What protects the fabric instead:

- the relay accepts events only from **allowlisted pubkeys** (step 4);
- every incoming signature is **verified fail-closed** by the recipient;
- `mailbox doctor` refuses to agree an avatar is itself unless the injected key
  derives the pubkey `registry.json` claims for it — a swapped key fails loudly
  rather than misdelivering silently.

**Revisit this the moment avatars run on hosts their operator does not own.** That is
the change that makes key custody worth paying for again; under one operator on one
machine, who can impersonate any avatar anyway, it was not.

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

`mailbox doctor` **refuses to agree an avatar is itself** unless its own entry exists and
the injected key derives the pubkey that entry claims. That is deliberate: a wrong or
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
than assuming — `scripts/fleet-keys status` prints an `in relay` column per avatar.
`fleet-keys publish` does steps 3 and 4 across the whole fleet at once, which is what
you want when an identity changes: every avatar holds its own copy of the address
book, and missing one fails silently.

## 5. Bring up the shared relay (once)

```sh
cd <your-path>/applications/tools/nostr-relay
docker compose up -d
```

This creates the always-on relay + the shared `nostr-mailbox-net` network that every
avatar's mailbox joins.

## 6. Per-avatar mailbox

There is nothing to start. The mailbox is a shell script the agent runs, not a
service, because the relay holds mail until it is claimed and there is nothing for a
daemon to do in between.

Per avatar:

1. materialize the component's `mailbox` script and its `registry.json` (step 3) into
   `<avatar>/tools/nostr-mailbox/`, and install the `mailbox` skill;
2. make sure `nak` is in that avatar's harness image — it is the only binary
   dependency, and it does all the signing and relay traffic;
3. have the agent run `mailbox doctor`. It checks `nak`, asserts the injected key
   derives this avatar's registry pubkey, names the relay, and creates the Maildir.

Existing avatars with an `inbox/` and `outbox/` from the file-copy era are converted
once, host-side:

```sh
cd <your-path>/applications/tools/nostr-mailbox
scripts/to-maildir <avatar-dir> --dry-run     # then without --dry-run
```

It leaves the originals in place; removing them is a git operation you do deliberately.
