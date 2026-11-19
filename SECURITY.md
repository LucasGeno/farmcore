# Security

FarmCore is early-stage software (0.x). Only the latest release on `main` receives fixes.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this
repository. Do not open a public issue, and do not include real credentials or farm data in a
report. You will get an acknowledgement within a week; fixes are released as soon as they are
ready and credited unless you ask otherwise.

## Access model

Farm reads are private by default, including on localhost and when `ADMIN_TOKEN` is unset.
Local identities and explicit farm memberships are separate from the installation operator.
No public registration, email service or legacy grant-name mapping creates an account.
Numeric grant URLs are not credentials: ordinary sharing requires an active authenticated recipient
explicitly bound to the grant. This also applies to intentionally public demo farms.

| Who | Can |
|---|---|
| Installation operator (`ADMIN_TOKEN`) | Installation controls, all configured farms, global model-call audit and legacy capture |
| Farm owner | Read/export assigned farms, import machine operations, log and confirm sprays from the assistant panel on the selected farm, preview/confirm/cancel corrections of confirmed sprays, set production/inventory thresholds, assign/revoke existing local identities and recipient-bound grants |
| Farm advisor | Read/export assigned farms and finalized correction history, and answer recommendation-review requests assigned to them with an opinion and note; no spray capture, corrections, arbitrary operation writes, membership administration or installation controls |
| Farm owner or advisor: advisor parameters | Read the farm's parameter catalog and version history and saved assessment evidence; propose a bounded parameter version, record an approved/unsure/rejected opinion, and activate an approved version for that farm, crop, region and year. Each step needs CSRF and rechecks the membership under the farm lock |
| Anonymous visitor | Health, static files and sign-in; no private farm reads or writes |
| Explicit demo visitor | Read only explicitly designated demo farms; no model-call audit |
| Tour visitor | Same farm read authority as the underlying identity; every mutation refused except leaving the tour (exit, sign-out) |
| Grant recipient | Read only active explicitly bound grant resources; no farm membership, audit, raw transcripts or pending capture |
| Agent token (personal access token) | Its grant's scopes on its grant's farm: ordinary grant reads and `drafts:propose`; never `*`, writes or audit |
| Numeric grantee URL | No authority without a valid authenticated recipient session |

Any signed-in principal, a grant recipient included, may read "About this model" (`/models`,
`/models/{key}`): it is static model specification (method, sources, validation level) with no
farm data, and the validation badge beside every shared result links to it.

One member may propose, approve and activate their own parameter version: small farms often
have a single owner, so a second reviewer is not required. History is append-only and records
who did each step and when (`created_by` on the version, `reviewer` on each opinion, `actor` on
each activation), so a self-approved version is visible as such. An approval is a recorded human
opinion, not evidence of field accuracy. Advice-only or weather-only grants cannot read parameter
history (`parameters:read`).

Provision identities locally with `farmcore users create <login> --farm-id <id>`; this prompts
for a hidden, confirmed password. Omit `--farm-id` to create an account without farm authority.
For automation only, `--password-stdin` reads the password from standard input. There is no
password argument or environment-variable setting. Owners assign existing accounts through
**Farm members**. Do not use grant names as account names or infer authority from them.

Member credentials use unique random salts and scrypt (`N=131072, r=8, p=1`, 32-byte digest),
following [OWASP password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Passwords must contain 15–256 characters. Credential checks take an installation-wide PostgreSQL
advisory lock, bounding expensive hash work across web processes to one check at a time
(128 MiB nominal memory, 192 MiB hard scrypt allocation limit). This deliberately favors bounded
resource use over high login throughput.

Member sessions are random 32-byte opaque tokens; only SHA-256 hashes are stored in the
server-side session table. The credential and CSRF cookies are HttpOnly, Secure,
SameSite=Strict, Path=/; sessions expire after 12 hours of wall time, independently of replay.
Logout revokes the database session, clears the cookies and opens `/login`. Membership
revocation, disabled identities, expired/revoked sessions and forged tokens deny the next
request, including when the farm is also explicitly public demo data. Authentication is never
cached between requests. Local recovery commands:

```sh
farmcore users disable <login>
farmcore users revoke-sessions <login>
farmcore users set-password <login>       # hidden prompt; revokes sessions and re-enables identity
```

Login attempts reserve a committed PostgreSQL budget before credential verification, including
failed requests: 5 per canonical login, 20 per socket peer, 100 installation-wide per 10 minutes.
Fixed hashed slots cap storage at 2049 counters; collisions conservatively share a budget.
Forwarding headers do not select limiter identities. Limits survive application restart and
multiple processes; legitimate logins also consume budget. Proxy-level limits remain useful.

Cookie-authenticated member mutations require both same-origin proof and a server-bound CSRF
nonce (hidden `csrf_token` field or `X-CSRF-Token` header). Plain forms and HTMX uploads use the
same checks. Operator-cookie mutations require same-origin proof; explicit `X-Admin-Token`
requests remain available for trusted scripts. Never put credentials or tokens in a URL.

- `/health` is public by design (monitoring and the container healthcheck use it). It reports
  the installation id, the running release, the database's Alembic heads and the upgrade state,
  and nothing else: no hostname, connection string or secret. If you would rather not disclose
  the release, have the reverse proxy answer `/health` only to your monitoring.
- Set `ADMIN_TOKEN` with `openssl rand -hex 24`. `FARMCORE_PUBLIC=1` requires at least
  32 characters without whitespace and disables interactive API docs. Missing tokens never
  grant operator privileges; provisioned member accounts remain independent.
- The operator signs in at `/admin/sign-in`; its cookie contains the token itself
  (HttpOnly, Secure, SameSite=Strict, 12 h). Rotating the token signs operators out.
- Public showcases require both `DEMO_MODE=true` and `DEMO_FARM_IDS=[1,2]` (actual IDs of
  intentionally public farms). Seeding alone does not publish a farm. Use a dedicated showcase
  database. A forged selector or tour cookie cannot publish other farms.
- Global model-call audit and the legacy shared web drafts (written before capture was bound)
  remain operator-only, and only on the default farm. Member activity/history excludes unbound
  pending draft metadata. The safe read-only assistant uses the authenticated principal and
  selected farm for tools and history.
- Web spray capture is open to farm owners and bound to the selected farm and the signed-in
  identity: drafts, clarifications and confirmations are keyed by both, another owner, farm or
  advisor cannot see or confirm them, and the owner's session and membership are rechecked under
  the farm lock before every write. Mutations need the CSRF token.
- Model policy differs by path on purpose. Spray capture uses the configured extractor (Gemini
  when set up) for owners too, because drafting a spray from free text needs it; the read-only
  assistant's model tool chooser serves only the operator, and members get the keyword router.
  Farms flagged `no_model` never reach a model on either path. The assistant's per-minute quota
  counts a signed-in member by identity (clearing the cookie does not reset it) and anonymous
  browsers by cookie.

Farm owners issue agent tokens (personal access tokens) from **Sharing** for their own AI
agents. Each token is a grant: its farm, scopes, purpose and end date (at most 366 days) are
shown there with when it was last used and its reads in the access log, and **Revoke** ends it.
A token holds only scopes an ordinary grant may read plus `drafts:propose`; never `*`, a write
scope or `audit:read`. It is 32 random bytes behind a `fc_pat_` prefix, shown once on the page
that creates it (`Cache-Control: no-store`) and stored only as its SHA-256: a slow KDF guards
guessable secrets, and 256 random bits are not guessable. Every call checks the token afresh
(never cached): a revoked or expired grant, another farm, or an issuer who is no longer an
enabled owner of the farm refuses the next call with one message for every cause. Restore never
loads tokens and revokes their grants, so a token revoked after a backup does not come back and
the copy starts with no agent access; issue new tokens on the copy.
Send tokens in a header, never in a URL.
### MCP over stdio is an operator transport

`farmcore mcp` (MCP over stdio) reads the agent token from `FARMCORE_TOKEN` only, never a flag,
and never prints or returns it. The token bounds what the agent does **through MCP tools**: its
farm, its scopes, `drafts:propose` for any write, an `access_log` row per call. It is **not** a
boundary against whoever runs the process. The process holds `DATABASE_URL`, and the runtime
role reads and writes every farm on the installation: anyone (or any agent with a shell) that can
read that URL, from a client's config file or the process environment, can read every farm, mint
tokens and rewrite the access log, and token scoping no longer applies.

- Treat stdio as installation-operator access. Never give it to a farmer who is not the
  operator, or to a third party; they get remote MCP over HTTP with a token (below).
- Keep `DATABASE_URL` inside the container: point the client at
  `ssh <host> docker compose -f <path>/docker-compose.yml exec -T -e FARMCORE_TOKEN app
  farmcore mcp`, with only `FARMCORE_TOKEN` in the client's config, never `DATABASE_URL`. ssh
  forwards the variable only with `SendEnv FARMCORE_TOKEN` on the client and
  `AcceptEnv FARMCORE_TOKEN` on the host; never put the token on the command line, where `ps`
  and shell history show it.
- Do not run a shell-capable agent on a machine whose files or environment hold `DATABASE_URL`.
- Use the runtime role's URL, never the database owner's.

### MCP over HTTP is for remote agents

`farmcore mcp --http` serves the same tools over Streamable HTTP at `/mcp`. It is off unless
`MCP_HTTP_ENABLED=true` and listens on `127.0.0.1` unless `MCP_HTTP_HOST` says otherwise. The
process holds `DATABASE_URL` as the web app does; the agent holds only its token, so this is the
transport for anyone who is not the operator. Each request is checked before any MCP message is
read (`core/mcp_http.py`):

- **Token.** `Authorization: Bearer <token>` only. A request with any query string is refused
  (400) without being authenticated, so a token pasted into a URL never works and never reaches
  a log. Missing or wrong: 401. Every tool list and call re-checks the request's own token, so a
  revoked or expired token fails its next request; the server keeps no session to take over and
  no response stream (stateless, JSON responses; GET and DELETE get 405).
- **DNS rebinding.** `Origin` must be in `MCP_HTTP_ALLOWED_ORIGINS` when present (403) and `Host`
  in `MCP_HTTP_ALLOWED_HOSTS` (421; the loopback names by default). The server never sends CORS
  headers, so a browser page can neither read a response nor preflight a token-bearing request.
- **Limits** (assumed product values): `MCP_HTTP_RATE_PER_IP` requests a minute per client
  address (checked before any token lookup), `MCP_HTTP_RATE_PER_TOKEN` per token (429 with
  `Retry-After`), `MCP_HTTP_MAX_BODY_BYTES` per request (413), `MCP_HTTP_MAX_CONNECTIONS`
  concurrent connections (503). Counters live in the process: several processes multiply them.
- **Logs.** uvicorn's access log is off; each refusal logs one INFO line with its status and
  the client address, never a header, URL or body.
- **Slow requests are not timed out.** A connection that sends half a request, or nothing, stays
  open: `MCP_HTTP_MAX_CONNECTIONS` of them, from one unauthenticated client and before any rate
  limit, hold every slot and every other request gets 503. The 5 s keep-alive timeout only
  starts after a response.
- **Client address.** The per-address limit trusts `X-Forwarded-For` only from the addresses
  in `MCP_HTTP_FORWARDED_ALLOW_IPS` (empty by default: the socket's peer is the client). Never
  list loopback on a shared host: any local process could then rotate its address.

Beyond loopback a **buffering reverse proxy with TLS and header and body timeouts is required**,
not optional: TLS because the token is a bearer secret, the timeouts because of the slow-request
limit above. Do not expose `MCP_HTTP_HOST=0.0.0.0` directly. Set `MCP_HTTP_ALLOWED_HOSTS` to the
public host name and `MCP_HTTP_FORWARDED_ALLOW_IPS` to the proxy's address, and make the proxy
append or overwrite `X-Forwarded-For` (nginx `$proxy_add_x_forwarded_for`, Caddy's default),
never pass a client's value through.

### OAuth for MCP lets an owner approve an agent

With `MCP_OAUTH_ENABLED=true` the web app is also an OAuth 2.1 authorization server for the
MCP endpoint (`core/oauth`; the issuer is `PUBLIC_BASE_URL`), so a remote agent gets a token by
its owner's consent instead of a pasted one. Both URLs must be https (http only to loopback),
and `PUBLIC_BASE_URL` has no path (metadata is served at the host's root).

- **Discovery.** `/mcp`'s 401 names its resource metadata (RFC 9728), which names this server;
  its metadata (RFC 8414) advertises S256 PKCE only and `iss` on authorization responses.
- **Clients** are public (no secret) and come from three places, in this order: the operator's
  `MCP_OAUTH_CLIENTS`; an https `client_id` that is the URL of the client's metadata document
  (Client ID Metadata Documents, the MCP spec's preferred way); or open Dynamic Client
  Registration at `/oauth/register` (RFC 7591), capped per address and while unused, issuing
  IDs that never start with `https://`. An unknown client, unreadable metadata or a
  `redirect_uri` that is not a registered one gets a 400 page and no redirect, so the server is
  never an open redirector. Matching is exact, except that a loopback redirect URI
  (`127.0.0.1`, `[::1]`, `localhost`) may name any port (RFC 8252 §7.3), as local agents need.
- **Metadata fetches** are a server-side request the client chooses, so they are fenced: https
  only, the name resolved once and every address public (no loopback, private, link-local,
  shared, multicast, mapped or NAT64 address), the connection pinned to that checked address
  (no second DNS answer), no redirects, no proxy, no cookies, JSON only, 5 KB and a 5-second
  deadline. A document counts only if its `client_id` is the URL it came from, byte for byte,
  and it asks for no secret. Valid documents are cached 5 minutes to a day (Cache-Control
  within those bounds); failures are never cached. Anyone can make the server fetch a public
  URL this way (the authorize page does it before sign-in), at most 5 KB each.
- **Codes.** Single use, 5 minutes, bound to the client, `redirect_uri` and an S256 challenge
  (`plain` is refused). A code presented again with its valid client, `redirect_uri` and verifier
  ends the connection it created (RFC 6749 §4.1.2); a replay with wrong parameters ends
  nothing (OAuth 2.1 §7.5.3), so seeing a used callback URL is not enough to disconnect an
  agent. A token request naming another resource gets `invalid_target`.
  Every authorization response, success or error, carries `iss` (RFC 9207: mix-up attacks).
- **Consent** is a signed-in owner's of the farm they choose, CSRF-checked like every form, and
  never a tour's, and the page refuses to be framed (`X-Frame-Options`, `frame-ancestors`). The
  page names the client, where the browser goes back to and each requested
  scope in plain words. A name the client gave itself is marked self-reported, with the host
  its metadata came from; a redirect to this computer warns that any local program could
  claim it; no client logo is shown; the token gets the requested scopes the owner left ticked, never more, and never more
  than an agent token may hold (read scopes and `drafts:propose`).
- **Tokens** are opaque and stored hashed. An access token lasts an hour and is accepted only by
  the MCP endpoint whose public URL (`MCP_HTTP_PUBLIC_URL`) it was issued for (RFC 8707): never
  on stdio, never by another resource. Refresh tokens rotate; reusing a rotated one, or
  revoking either token (RFC 7009; only by the client it was issued to), ends the connection.
  Parallel refreshes with one refresh token count as reuse too. The connection is a grant on Sharing
  (actor `token:<id>`, 90 days at most) and revoking it there ends it too. Every expiry is set
  and checked by this server's clock alone.
- **Sign-in cookies are `SameSite=Strict`**, so arriving from the agent's site the owner signs
  in again before consenting.

## Deployment checklist

- Serve over HTTPS only (the admin cookie is `Secure`) behind a reverse proxy. Keep the app
  bound to `127.0.0.1` (the compose default) and set `FORWARDED_ALLOW_IPS` to the proxy's
  address so client addresses are correct.
- Keep PostgreSQL off the network: the compose file publishes no database port. Use long,
  different random `POSTGRES_PASSWORD` (the owner: only `migrate` and `backup` get it) and
  `RUNTIME_DATABASE_PASSWORD` (the DML-only role the app and bot use).
- Limit request size, rate and timeouts at the proxy. ISOXML parsing has its own limits, but
  multipart parsing and speech transcription use CPU and memory before application checks.
- Telegram: keep `TELEGRAM_ALLOWED_USER_IDS` set (empty means nobody); run one bot per token.
- Vertex AI: use a service account that can only call `aiplatform.endpoints.predict`, mount its
  key as a file (`docker-compose.vertex.yml`), and set budget alerts.
- Back up with the `backup` service (set `BACKUP_RECIPIENT`, the public half of an age key you
  keep off the server) and test a restore before relying on the data.
- Only install apps you trust: an app is Python code running with full database access.

## Backups and restore

A backup bundle is encrypted with [age](https://age-encryption.org) to the owner's public key:
only the private key reads it. age gives confidentiality, not authenticity: the public key sits
in the server's environment, and anyone who has it can encrypt a bundle of their own. So a
bundle is data only, never code:

- `farmcore restore` never runs a bundle's SQL as the database superuser. The schema comes from
  this release's own migrations; a dump holding anything that schema lacks (a function,
  trigger, view, rule, extra table, index or schema) is refused before any row is loaded.
- Rows are loaded with `pg_restore --data-only` connected as a temporary login role created for
  that restore: not a superuser, no role membership, no `CREATE` or `TEMP` anywhere, `INSERT`
  only on FarmCore's tables and `UPDATE` only on their sequences, `CONNECT` only to the staging
  database. Whatever SQL an archive carries in its data entries runs with those rights only. The
  role is dropped (with anything it owns) before the copy goes live.
- Member sessions and access tokens are never loaded, database settings an archive records are
  never applied, and the copy starts with a new `installation_id` and external effects paused.
- The archive is read strictly: two plain files, no links, devices or sparse files, nothing
  larger than the archive itself. Strings from the bundle reach the terminal escaped.
- Restore revokes `CONNECT` and `TEMP` from PUBLIC on every database of the server but
  `template0`, permanently (as `migrate` does), so the loader role opens only the staging
  database. One restore runs per target; SIGTERM and SIGHUP still clean up, a signal during
  the swap waits for its end, and the next restore removes what a SIGKILL left (its staging
  database and loader role, and its temporary directory once six hours old).

What restore cannot prove is that the rows are the ones you backed up, and the rows are what
grants access: members and their password hashes, farm roles and memberships, sharing grants,
app states and settings, and the farms' links to Telegram chats are all rows. Whoever forges a
bundle chooses who can sign in to the copy (as its owner, with a password they know) and, once
effects are enabled, which chats its advice push writes to. A bundle someone hands you is as
untrusted as one replaced on shared storage: restore only bundles you wrote and kept where
only you can write. Old plain `.dump` files from earlier releases are not bundles and are
restored by hand as the superuser, so only restore dumps you made and kept yourself.

For migration authors: after a restore, `migrate` runs every newer migration as the superuser
over those restored rows. A migration must never put a row's value into SQL text (no string
formatting of selected values into `op.execute`); use set-based statements or bind parameters.

## Known limits

Owners manage grants from **Sharing**, choosing an existing enabled recipient login separately
from the display name. Create recipients with `farmcore users create <login>` without `--farm-id`
when no membership is intended. Those users sign in at `/login` and land on `/share/received`;
only `/share/as/<grant-id>/` and its authorized exports expose shared data. Farm selectors,
notifications, ordinary assistant tools and other farm pages never inherit grant authority.
Owners can preview their own grants with the same scope and real-time validity restrictions.
Legacy unbound grants remain stored but confer no recipient authority; no display-name backfill
occurs. Replace them with explicitly bound grants. Expiry, revocation and disabled identities
deny the next read, independent of the replay clock. Reads preserve user, farm, grant and purpose
in the owner's access log. Owners can inspect only their own farm's consent ledger.

The advice-only preset exports stored facts and a whitelisted configuration identifier/review
status; it never exports parameter snapshots, reviewer identity/notes or raw weather. Advice
consent does not imply logs, stations or private parameter/correction history. Grant repositories
fail closed for unlisted app resources, audit and writes even when legacy scope arrays are broad.
There is no generic grant model-table export endpoint.
Spray corrections keep the original record, its source receipts and saved weather in an audit
snapshot with the reason, actor and time. Correction pages and JSON show only the corrected
facts and each source's type and time, never transcripts, chat ids or machine files. A pending
correction is visible only to its author; finalized ones to the farm's members. Once confirmed or
cancelled, a correction is immutable by application rule only: the database role can still
update the row.
Advisors answer owner recommendation-review requests with an opinion and note; a review never
rewrites the stored assessment or substitutes for agronomic validation.
The operator-token sign-in limiter remains per-process and in memory; ordinary member login
uses the persisted budgets above. No independent validation of the agronomic rules is claimed.
Advice is decision support; product labels and local regulations take precedence.
