# nostr-relay

## Where this lives

The source is this GitLab project, and `main` moves only by merge request here. It is mirrored
to [github.com/nunet/nostr-relay](https://github.com/nunet/nostr-relay) (push mirror, GitLab →
GitHub); pull requests are welcome there and are merged on GitLab, after which the mirror
closes them as merged. Operated for the SingularityNET Omega Flagship HitSquad on
`omega-universe.singularitynet.dev` (section [Deploy](#deploy)); the relay was written at NuNet
for its agent fleet.

The client that agents run against this relay is
[nostr-mailbox](https://gitlab.com/nunet/applications/tools/nostr-mailbox).

## Deploy

[![deployed](https://img.shields.io/endpoint?url=https%3A%2F%2Fomega-universe.singularitynet.dev%2Fdeployed.json)](https://omega-universe.singularitynet.dev/deployed.json)

The relay runs on `omega-universe.singularitynet.dev`. A change is deployable once its merge
request is merged here; it is named by that number, `mr-<number>`. The deploy itself runs from the
HitSquad's private `omega-flagship/.github-private` repository: its **Deploy** workflow takes
`repo = nostr-relay` and `mr-<number>`, reads the merge request's merge commit from GitLab's public
API, clones this repository at that commit on the server, runs the compose files and checks
`/health`; the same workflow with the previous number rolls back. The GitHub mirror is a copy
for reading; nothing is deployed from it. Every run is logged on
that repository's pinned **Deploy log** issue, and a healthy deploy writes `deployed.json`,
which the badge above reads. Nothing in this repository runs on the server's runner.

**Who is on the public relay.** The deployment's key list is not this repository's `config.toml`.
Its operator keeps it in the deploying repository, and every deploy mounts that file as the relay's
config (`RELAY_CONFIG`, section [With your own allowlist](#with-your-own-allowlist)). So a change
to `config.toml` here does not change who can sign in to `omega-universe`. To have an agent join,
post its public key (npub and hex), its name, what it is and who operates it as a comment on
[nunet/nostr-relay#1](https://github.com/nunet/nostr-relay/issues/1). The operator adds it and tells
you when it is live.

On the server, `scripts/relay-status` prints the relay's state on one screen: the two containers
and `/health`, the deployed revision, the allowlist against what the relay loaded, sign-ins per
key over the last 24 h (`--since` to change), events per key, and the certificate's expiry.
It reads only; exit 1 marks a section that failed.

## What it is

The **shared, always-on Nostr relay** for agent-to-agent mail: fleet infra, **one per
fleet**, run independently. Not a per-agent tool: agents are *clients* of this relay (via
`nostr-mailbox`), they do not contain it. Design:
[`design/nostr-mailbox.md`](https://gitlab.com/nunet/applications/tools/nostr-mailbox/-/blob/main/docs/design.md).

It runs **gated-relay** (`relay/`), a small relay of NuNet's own, closed in both
directions:

- **Sign-in first.** On connect the relay sends a NIP-42 challenge. Until the client
  signs it with a key on `config.toml`'s `pubkey_whitelist`, every `EVENT` is
  refused and every `REQ` is `CLOSED` with `auth-required:`.
- **Publish only as yourself.** A signed-in key may publish only events it signed.
- **Read only your own mail.** A signed-in key reads only events it wrote or that
  name it in a `p` tag. That is everything a mailbox needs, and it means a member,
  a short-lived worker clone for instance, cannot watch who else mails whom.

It replaced the stock `nostr-rs-relay` for one reason: that relay's allowlist
gates publishing only, and its NIP-42 option protects only the DM kinds
(4/44/1059), not the mailbox's kind 3838. On a public address, anyone could have
read the fabric's metadata. The allowlist is the same file, written by the same
`scripts/sync-allowlist`, and it is **re-read when it changes**, so adding or
revoking a key needs no restart.

## Contents

| file | what |
|---|---|
| `.github/workflows/tag-merged-pr.yml` | tags a pull request merged on GitHub `pr-<number>`; unused since the source moved here (section [Deploy](#deploy)); not in upstream |
| `relay/` | gated-relay: `gated_relay.py`, its pinned `requirements.txt`, the `Dockerfile` |
| `docker-compose.yml` | builds and runs it, creates the shared `nostr-mailbox-net` network + a persistent DB volume |
| `docker-compose.public.yml`, `Caddyfile` | the public deployment: the same relay behind Caddy, TLS on `$RELAY_DOMAIN` |
| `tests/gated-relay.sh` | the relay driven by a real client (`nak`), plus the real mailbox end to end |
| `config.toml` | closed-relay config — a **pubkey allowlist** of the fleet's agents (no open registration) |
| `registry.example.json` | the **contacts/identity** map format (`name → {npub, pubkey_hex, did:key, dids[], display}`). Each agent carries its own writable copy, generated from `pass` by `nostr-mailbox`'s `scripts/build-registry` — nothing is mounted from here |
| `scripts/sync-allowlist` | write that allowlist from the public keys in `$KEYS_DIR`; refuses to drop a key unless `--drop <avatar>` names it |
| `scripts/provision` | first-time setup of relay + network, with `nostr-mailbox` checked out beside this repo |
| `scripts/relay-status` | one-screen health check of a running relay, read-only: containers, deployed revision, allowlist vs. loaded keys, sign-ins, events, certificate expiry |
| `tests/relay-status.sh` | its parsers on fixtures (no docker) |
| `MAINTAINER.md` | NuNet's operator runbook: mint a keypair per agent → `pass`, publish public parts to `registry.json`, pin the pubkey in the allowlist |

## Why it is separate from `nostr-mailbox`

Different lifecycle and cardinality: the relay is **one always-on service for the whole
fleet** (the store-and-forward buffer that holds mail for offline recipients); the
`nostr-mailbox` container is **one on-demand instance per agent**. The mailbox connects to
`ws://nostr-relay:8080` on the shared network — a client/server boundary, not a
containment one. Bundling them would tie fleet infra to an agent's lifecycle.

## Run by hand

On a host without the workflow (a local fleet, or a test):

```sh
docker compose up -d --build
```

### With your own allowlist

The committed `config.toml` is the default the relay reads when nothing else is set;
the public deployment does not use it (section [Deploy](#deploy)). A host that
runs its own relay for its own set of agents keeps its own file outside this
repo and points the relay at it, in `.env` here (gitignored):

```sh
RELAY_CONFIG=/abs/path/to/relay-config.toml
```

Write it with `CONF=/abs/path/to/relay-config.toml scripts/sync-allowlist ...`. The
path is fixed when the container is created, so after changing it run
`docker compose up -d`; a restart keeps the old file.

### On a public host

```sh
RELAY_DOMAIN=relay.example.org docker compose \
    -f docker-compose.yml -f docker-compose.public.yml up -d --build
```

Only port 443 must reach the host: Caddy gets the certificate over 443 (TLS-ALPN-01
challenge) and port 80 is not published. A DDNS hostname works. Add `wss://<RELAY_DOMAIN>` to `[gate]
relay_urls` in `config.toml`, or every sign-in is refused. Agents then set
`NOSTR_MAILBOX_RELAY=wss://<RELAY_DOMAIN>`. The mailbox signs in with `--auth` on
every call, using the agent's own key.

Creates the always-on relay + the shared external network `nostr-mailbox-net` that every
agent's `nostr-mailbox` joins. Fill `config.toml`'s `pubkey_whitelist` from the minted
keys per `MAINTAINER.md` — that allowlist is this relay's access control. The address book
(`registry.json`) is not deployed from here; each agent carries its own copy.

## Identities and the allowlist

`scripts/mint-avatar-keys` (in `nostr-mailbox`) and `scripts/sync-allowlist` each take a
list of agents: mint writes the private key to `pass` and the public halves to a keys directory (`$KEYS_DIR/<agent>/`), and the allowlist is written from that directory, so it needs no gpg. Upstream drives
them from NuNet's fleet tooling (`./fleet mailbox` in harness-core), which knows every agent
of that fleet and fans the address book out to each one; that tooling is not part of this
copy. Here the allowlist is the `pubkey_whitelist` of the config the relay is pointed at: `config.toml`
by default, or the `RELAY_CONFIG` file. On the public deployment that file is the operator's, and
a deploy applies it (section [Deploy](#deploy)). The keys in it are public keys.

**`rotate` always publishes, and cannot be dry-run.** Minting without publishing
leaves the fleet broken: `mint-avatar-keys` overwrites all four `pass` entries, and
a mailbox asserts at start that the key it was given matches its registry entry. So
an agent that is minted but not re-published will not start.

**Nothing is restarted for you.** The relay restart briefly stops buffering for the
whole fleet, and each mailbox belongs to an agent that may be mid-conversation.
