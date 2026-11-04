# Hosted fleet

An operator running several [installations](self-hosting.md) declares them in a private
**fleet inventory**. `farmcore fleet plan` validates that inventory and reports what it declares.
It is fleet tooling for the operator, separate from farm request handling: the web app never
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

## Private state and ownership

Provisioning is a separate command, which takes the contract below as given. Nothing here exists
until then, and planning neither creates nor checks it.

The operator names one absolute `--state-root` on each command; there is no default. It is
private to the operator (mode `0700`), outside every checkout, and laid out as:

```text
<state-root>/
  fleet.lock                     one apply at a time
  secrets/<slug>/<file>          what `secrets.*` refer to: regular files, mode 0600, no symlinks
  installations/<slug>/          generated Compose env file and the ownership record
    ownership.json               slug, Compose project and a random owner id written at creation
```

A resource belongs to the fleet only if all of this holds:

- The Compose project is exactly `farmcore-fleet-<slug>`, derived from the slug and never declared.
  Its network and volumes are then named `farmcore-fleet-<slug>_*`.
- `installations/<slug>/ownership.json` exists and names that slug and project.
- Each Docker resource carries that project's Compose label and the record's owner id as a label.

Provisioning must never adopt what fails this: a container, network or volume of that name
without the record and labels, or a port another process holds, stops the run with a diagnosis
before anything changes. Those live checks belong to the provisioning preflight because a plan
reads no live state. A record whose resources have drifted also stops the run rather than being
repaired by guesswork, and a repeated apply reuses existing secrets, never rotating them.
