# Security

FarmCore is early-stage software (0.x). Only the latest release on `main` receives fixes.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this
repository. Do not open a public issue, and do not include real credentials or farm data in a
report. You will get an acknowledgement within a week; fixes are released as soon as they are
ready and credited unless you ask otherwise.

## Access model

FarmCore has **one administrator per installation** and no user accounts.

| Who | Can |
|---|---|
| Administrator (`ADMIN_TOKEN` holder) | Everything: replay clock and demo controls, app install/uninstall, ISOXML upload, grant create/revoke, stock changes, the model-call payloads, debug views, the web assistant's model and voice input |
| Anonymous visitor | Read every farm page and export of the installation; no writes. Reads are recorded in the access log |
| Tour visitor (`/tour`) | Like an anonymous visitor, and every non-GET request is refused, even with the admin token |
| Grantee link (`/share/as/<id>/`) | Read the grant's scopes; links are not authenticated, so anyone with the URL can open them |

Consequences you should plan for:

- **Everything the installation holds is readable without signing in.** Do not load data you
  are not willing to publish on a network-reachable install, or put the whole site behind your
  own authentication (reverse-proxy basic auth, an identity-aware proxy, a VPN).
- **Without `ADMIN_TOKEN`, every visitor is administrator.** That mode is for local
  development only. `FARMCORE_PUBLIC=1` (always set by `docker-compose.yml`) makes the app refuse
  to start without a token of at least 32 characters with no whitespace, and turns off the
  interactive API docs. Generate the token with `openssl rand -hex 24`.
- The administrator signs in at `/admin/sign-in`; the session cookie holds the token itself
  (HttpOnly, Secure, SameSite=Strict, 12 h). Scripts send `X-Admin-Token`. Rotating the token
  signs every browser out. Never put the token in a URL.
- Sign-in failures are throttled per client in memory (per process, not distributed). Security
  rests on the token's entropy, not on the limiter.

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

No per-user accounts or multi-tenant isolation, no authenticated grant recipients, no
distributed rate limiting, and no independent validation of the agronomic rules. The advice is
decision support; product labels and local regulations take precedence.
