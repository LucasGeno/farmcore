# Hosted fleet

An operator running several [installations](self-hosting.md) declares them in a private
**fleet inventory**. `farmcore fleet plan` validates that inventory and reports what it declares;
`farmcore fleet apply` provisions one declared installation with the stock Compose file, and
`farmcore fleet status` reports how it is doing. It is fleet tooling for the operator, separate from farm request handling: the web app never
loads it, and it adds no database, portal or Docker socket.

## The inventory

One folder holding one `<slug>.yaml` per installation and nothing else; [examples/fleet](../examples/fleet)
is a synthetic one. Keep a real inventory in private storage, never in a public repository:
`scripts/check.sh` refuses tracked `fleet-inventory/` or `fleet-state/` folders, any tracked
`.yaml` or `.yml` declaration outside `examples/fleet` in any spelling the plan accepts, and any
example not on `<name>.example.test`. It names refused files and never prints their content.

| Field | Rule |
|---|---|
| `schema_version` | `1` |
| `slug` | 1–32 lowercase letters, digits and single hyphens, starting with a letter; the file's name |
| `domain` | One lowercase host name under an allowed suffix: no wildcard, port, scheme, IP or `xn--` label |
| `images.app`, `images.database` | Pinned as `<repository>@sha256:<digest>`; no tag |
| `binding.host` | An IPv4 loopback or private address (RFC 1918, or 100.64.0.0/10) |
| `binding.port` | 1024–65535, once across the inventory |
| `limits.*_memory_mib` | 256–65536, for `app` and `database` |
| `limits.*_cpu_millis` | 100–64000, for `app` and `database` |
| `secrets.postgres_password`, `.runtime_database_password`, `.admin_token` | A different relative path for each, inside `<slug>/`; never a value |

The limits' ranges catch typing mistakes. They say nothing about what a host can carry.

The files are read as data only. Mappings with string keys, strings and decimal integers are all
there is: tags, directives, anchors, aliases, merge keys, duplicate keys, several documents,
floats, booleans, nulls and dates are refused, as are unknown fields, symlinks, and any other
entry in the folder. A file is at most 16 KiB and an inventory at most 64 entries.

## Planning

```sh
farmcore fleet plan /private/inventory --allow-domain-suffix pilots.example.org   # repeatable
farmcore fleet plan examples/fleet --test-domains                                 # fixtures only
```

One of the two is required. `--test-domains` accepts `<name>.example.test` and nothing else, and
`example.test` is never accepted as an allowed suffix, so a fixture cannot pass as a real
installation or the reverse.

The plan is JSON on standard output, the same bytes for the same inventory. It describes the
**declaration, not live health**: nothing is read but the inventory folder, so it needs no
database or credentials and starts no process. Secret references are never opened, and the
plan lists only their names. A refusal exits 1 with no plan and lists every problem by file, field
or line (the first 50). It never repeats a value or an unknown key, so a secret pasted in the wrong
place stays out of terminals and logs.

A port is unique across the whole inventory, even on different addresses: the inventory does not
say which addresses share a host.

## Applying one installation

```sh
docker pull <each digest the declaration names>     # apply never pulls or builds
farmcore fleet apply  /private/inventory alpha --state-root /private/fleet-state --allow-domain-suffix pilots.example.org
farmcore fleet status /private/inventory alpha --state-root /private/fleet-state --allow-domain-suffix pilots.example.org
```

Run it from a checkout of the release you are deploying: `apply` uses that checkout's
`docker-compose.yml` and takes no other Compose file or override. Docker reaches it only as
`docker` commands with structured arguments, and with Docker's own settings alone from your shell:
a stray `POSTGRES_PASSWORD`, `COMPOSE_PROFILES` or `TELEGRAM_BOT_TOKEN` there changes nothing. It
needs a Docker Compose that can parse the stock file; this was tested with 2.40.3.

`apply` starts the database, pauses external effects and reads that back, migrates, and only then
starts the app, so an installation never runs unpaused, even if the apply is killed halfway. After that
the setting is yours: a repeated apply reports what `farmcore effects` says and does not pause again.
Telegram, the AI extractor and voice stay off, and the clock stays on replay; the generated env
file sets none of them. `PUBLIC_BASE_URL` is `https://<domain>`. Routing, certificates and DNS are
yours to set up; `apply` publishes the app on the declared binding and touches nothing else.

Limits apply to each container: the app's to `app`, `migrate` and `backup`, the database's to `db`.
The stock Compose file leaves them off unless set, so a self-hosted installation is unaffected.

### What it reports

JSON on standard output: the slug, the state, the installation id, release and Alembic heads, the
external-effects setting, and any problems. Secrets are never in it; a failed step's Compose error
is bounded and redacted, and resolved Compose configuration is never read. Each `apply` that
gets past the state-root checks also appends one such line, with a timestamp, to `receipts.jsonl`.

| Exit | State | Meaning |
|---|---|---|
| 0 | `ready` | Answering on the binding as the saved installation, and Docker agrees |
| 1 | refused, on standard error | No Docker resource was started or changed; the message says why |
| 3 | `not ready` or `not provisioned` | Provisioned or started as far as the problems say; run `apply` again once fixed |

`ready` needs all of this: the app and database running and healthy; `/health` on the binding
answering with the saved installation id, release and Alembic heads and state `ok`; and each
container having the declared image, limits, its own network alone and exactly the declared
binding. Creation is not readiness. A healthy-looking answer that disagrees with the saved identity is `not ready`.

### Repeating, stopping and retrying

Applying again keeps credentials, ids and data, and leaves running containers alone. Stopping
one installation does not touch another, and `apply` starts it again.

If an apply is killed, run it again; it finishes the same installation. While a killed apply's Compose is
still running, the retry is refused until that Compose exits. `status` says `not ready` until an apply has finished.

## Private state and ownership

The operator names one absolute `--state-root` on each command; there is no default. Create it
yourself with mode `0700`, as its real path, outside every Git checkout. `apply` refuses it otherwise.

```text
<state-root>/
  secrets/<slug>/<file>          what `secrets.*` refer to: generated if absent, mode 0600
  installations/<slug>/          mode 0700
    ownership.json               ownership evidence; a salted digest of each secret, never a value
    compose.env                  generated, mode 0600: holds the secrets, never share it
    apply.lock                   one apply per installation at a time
    receipts.jsonl               what each apply concluded
```

A missing secret file is generated once, before an installation first uses it. You may provide your
own beforehand: 32 to 256 letters, digits or `. _ ~ -`. Every folder above it must be a real
directory of yours with mode `0700`, and the file a regular file with mode `0600`.

The Compose project is exactly `farmcore-fleet-<slug>`, derived from the slug and never declared, and
its network and volumes are named `farmcore-fleet-<slug>_*`. Labels can be copied, so a label alone
never makes a resource the fleet's:

- `pgdata` is created first, with a random owner id from the record, and read back. Of two state
  roots racing for one slug, exactly one finds its own id; the other stops.
- `ownership.json` saves each volume's creation time, the network's id and each container's id.
- A killed apply saved no container ids, so its containers also have to match the configuration
  hash Compose computes from this installation's env file.

### What `apply` refuses

Each of these stops before anything changes, and the resource stays as it was:

| Found | What to do |
|---|---|
| A volume, network or container of that name or Compose project, with no record | Move it away, or use the state root that owns it |
| Unknown files in `installations/<slug>/` with no record | Move them away |
| A declared port something else is listening on | Free the port or change the declaration before the first apply |
| A declared image not on this host | `docker pull` it |
| A changed declaration or Compose file | Restore it: upgrades and moves are not supported here |
| A missing or changed secret file | Restore it: the database still expects the original |
| A missing or replaced volume or network | Restore it from backup, or investigate; it is never recreated empty |
| A container this installation did not start | Remove it yourself once you know what it is; `apply` then recreates the service |
| Changed limits, another image, another network or a wider binding on a container | Put it back, or remove the container; `apply` then recreates the service |
| A record from another Docker daemon | Use that daemon |
| Another apply running | Wait |

There is no fleet command to upgrade, move, or delete an installation. For an upgrade, see
[Upgrading](self-hosting.md); `fleet apply` does not yet follow one, and refuses the changed
declaration. Nothing is ever deleted by these commands.

### What this does not protect against

Anyone who can reach the Docker socket or read the state root as the operator can read or forge
all of this. A creation time has whole seconds, and a limit in `status` is the configured value,
not proof of what happens at the limit.
