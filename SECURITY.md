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
Numeric grant URLs are not credentials; recipient authentication follows in the sharing phase.

| Who | Can |
|---|---|
| Installation operator (`ADMIN_TOKEN`) | Installation controls, all configured farms, global model-call audit and legacy capture |
| Farm owner | Read/export assigned farms, import machine operations, set production/inventory thresholds, assign/revoke existing local identities |
| Farm advisor | Read/export assigned farms; no arbitrary operation writes, membership administration or installation controls |
| Anonymous visitor | Health, static files and sign-in; no private farm reads or writes |
| Explicit demo visitor | Read only explicitly designated demo farms; no model-call audit |
| Tour visitor | Same farm authority as the underlying identity; every mutation refused |
| Numeric grantee URL | No authority; recipient binding is required before ordinary grant access opens |

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

- Set `ADMIN_TOKEN` with `openssl rand -hex 24`. `FARMCORE_PUBLIC=1` requires at least
  32 characters without whitespace and disables interactive API docs. Missing tokens never
  grant operator privileges; provisioned member accounts remain independent.
- The operator signs in at `/admin/sign-in`; its cookie contains the token itself
  (HttpOnly, Secure, SameSite=Strict, 12 h). Rotating the token signs operators out.
- Public showcases require both `DEMO_MODE=true` and `DEMO_FARM_IDS=[1,2]` (actual IDs of
  intentionally public farms). Seeding alone does not publish a farm. Use a dedicated showcase
  database. A forged selector or tour cookie cannot publish other farms.
- Global model-call audit, legacy shared web capture and its pending/history/draft paths remain
  operator-only. Member activity/history excludes unbound pending draft metadata. The safe
  read-only assistant uses the authenticated principal and selected farm for tools and history.

## Deployment checklist

- Serve over HTTPS only (the admin cookie is `Secure`) behind a reverse proxy. Keep the app
  bound to `127.0.0.1` (the compose default) and set `FORWARDED_ALLOW_IPS` to the proxy's
  address so client addresses are correct.
- Keep PostgreSQL off the network: the compose file publishes no database port. Use a long
  random `POSTGRES_PASSWORD`.
- Limit request size, rate and timeouts at the proxy. ISOXML parsing has its own limits, but
  multipart parsing and speech transcription use CPU and memory before application checks.
- Telegram: keep `TELEGRAM_ALLOWED_USER_IDS` set (empty means nobody); run one bot per token.
- Vertex AI: use a service account that can only call `aiplatform.endpoints.predict`, mount its
  key as a file (`docker-compose.vertex.yml`), and set budget alerts.
- Back up the database (the `backup` service) and test a restore before relying on the data.
- Only install apps you trust: an app is Python code running with full database access.

## Known limits

Recipient-bound sharing is not open in this phase: the sharing UI and routes remain operator-only.
The shared `WEB_CHAT_ID` capture/confirmation workflow remains operator-only until drafts and
confirmations are bound to a principal and farm; ordinary membership login does not unlock it.
Advisor recommendation-review writes arrive with the review feature; advisors currently read.
The operator-token sign-in limiter remains per-process and in memory; ordinary member login
uses the persisted budgets above. No independent validation of the agronomic rules is claimed.
Advice is decision support; product labels and local regulations take precedence.
