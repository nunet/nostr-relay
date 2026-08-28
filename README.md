# nostr-relay

The **shared, always-on Nostr relay** for the avatar mailbox fabric — fleet infra, **one
per fleet**, run independently. Not a per-avatar tool: avatars are *clients* of this relay
(via `nostr-mailbox`), they do not contain it. Design: [`design/nostr-mailbox.md`](https://gitlab.com/nunet/applications/tools/nostr-mailbox/-/blob/main/docs/design.md).

Deployment-only, like `slack-mcp` / `litellm`: it **pulls** the off-the-shelf
`scsibug/nostr-rs-relay` image — no source, no build. This repo versions the compose +
config + the shared identity registry.

## Contents

| file | what |
|---|---|
| `docker-compose.yml` | runs `nostr-rs-relay`, creates the shared `nostr-mailbox-net` network + a persistent DB volume |
| `config.toml` | closed-relay config — a **pubkey allowlist** of the fleet's avatars (no open registration) |
| `registry.example.json` | the **contacts/identity** map format (`name → {npub, pubkey_hex, did:key, dids[], display}`). Each avatar carries its own writable copy, generated from `pass` by `nostr-mailbox`'s `scripts/build-registry` — nothing is mounted from here |
| `MAINTAINER.md` | operator runbook: mint a keypair per avatar → `pass`, publish public parts to `registry.json`, pin the pubkey in the allowlist |

## Why it is separate from `nostr-mailbox`

Different lifecycle and cardinality: the relay is **one always-on service for the whole
fleet** (the store-and-forward buffer that holds mail for offline recipients); the
`nostr-mailbox` container is **one on-demand instance per avatar**. The mailbox connects to
`ws://nostr-relay:8080` on the shared network — a client/server boundary, not a
containment one. Bundling them would tie fleet infra to an avatar's lifecycle.

## Run (once, host-side; avatar-builder / operator)

```sh
docker compose -f docker-compose.yml up -d
```

Creates the always-on relay + the shared external network `nostr-mailbox-net` that every
avatar's `nostr-mailbox` joins. Fill `config.toml`'s `pubkey_whitelist` from the minted
keys per `MAINTAINER.md` — that allowlist is this relay's access control. The address book
(`registry.json`) is not deployed from here; each avatar carries its own copy.

## Fleet identities — mint, rotate, publish

`scripts/mint-avatar-keys` (in `nostr-mailbox`) and `scripts/sync-allowlist` each
take a list of avatars, so those two steps were never the hard part. The part that
goes wrong by hand is the **fan-out**: every avatar carries its **own copy** of the
address book, so changing one identity means rewriting `registry.json` in every
other avatar's directory and restarting every mailbox. Miss one and the failure is
silent — that avatar cannot verify or address the changed one, with no error until
someone sends mail.

`scripts/fleet-keys` owns that fan-out and the ordering.

```bash
scripts/fleet-keys status               # what pass, the relay and each avatar hold
scripts/fleet-keys publish --check      # what would change; writes nothing
scripts/fleet-keys publish              # allowlist + every avatar's registry.json
scripts/fleet-keys rotate <avatar>...   # mint NEW keys for these, then publish
```

The avatar list comes from the same `name = directory` map `sync-fleet` reads —
`$AGENT_FLEET_FILE`, or `--fleet FILE` — so where avatars live is maintained once
rather than in two files that drift.

**`rotate` always publishes, and cannot be dry-run.** Minting without publishing
leaves the fleet broken: `mint-avatar-keys` overwrites all four `pass` entries, and
a mailbox asserts at start that the key it was given matches its registry entry. So
an avatar that is minted but not re-published will not start.

**Nothing is restarted for you.** The relay restart briefly stops buffering for the
whole fleet, and each mailbox belongs to an avatar that may be mid-conversation.
The exact commands are printed at the end of a publish.

An avatar with no key in `pass` is left out of the address book and reported —
listing it would only invite a message that cannot be sent. An avatar with no
`tools/nostr-mailbox/` is skipped the same way.
