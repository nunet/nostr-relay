# Maintainer runbook — nostr-mailbox provisioning

Agents never mint or hold private keys. The **operator/maintainer** mints one keypair per
avatar, stores the `nsec` in `pass`, and publishes the public parts to `registry.json`.
avatar-builder wires the shared relay + network + injection. This file is the command
block to run **on the host** (not by an agent).

## 1. Mint a keypair per avatar

Use any Nostr keygen. With `nak` (https://github.com/fiatjaf/nak):

```sh
# per avatar, e.g. AVATAR=undergryd-software-engineer
nak key generate                 # prints the private key (hex)
SK=<hex-from-above>
nak key public  "$SK"            # pubkey (hex)  -> registry pubkey_hex
nak encode nsec "$SK"            # nsec1...      -> store in pass
nak encode npub "$(nak key public "$SK")"   # npub1... -> registry npub
```

did:key from the same secp256k1 pubkey (multikey `zQ3s…`, `0x2701` prefix) — derive with
the project's DID tool, or leave `did:key` blank in v1 and fill at nuActor integration.

## 2. Store the private key in `pass`

```sh
printf '%s' "$SK" | pass insert -m nunet/avatars/<avatar>/nostr-nsec
```

Wire it into the avatar's `harness/config` SANDBOX_SECRETS so launch injects
`NOSTR_MAILBOX_NSEC` (underscores only — shell-identifier rule). Also inject a shared
`MAILBOX_API_TOKEN` (any strong random string, same value into the sandbox + the mailbox
container).

## 3. Publish public parts to the shared `registry.json`

Add/update the avatar's entry (`npub`, `pubkey_hex`, `did:key`, `dids`, `display`) — see
`registry.example.json` (this component owns the live `registry.json`). It is shared,
read-only-mounted into every mailbox.

## 4. Pin the pubkey in the relay allowlist

Add each avatar's `pubkey_hex` to `pubkey_whitelist` in `config.toml`, then restart the
relay. Closed set = no spam/discovery problem internally.

## 5. Bring up the shared relay (once)

```sh
docker compose -f tools/nostr-relay/docker-compose.yml up -d
```

This creates the always-on relay + the shared `nostr-mailbox-net` network that every
avatar's mailbox joins.

## 6. Per-avatar mailbox

The avatar's own `launch` brings up `tools/nostr-mailbox/docker-compose.yml` (dual-homed:
avatar net + `nostr-mailbox-net`) with the injected env + host mounts (its `inbox/`,
`outbox/`, the shared `registry.json`). Install the `mailbox` CLI skill in the avatar.
