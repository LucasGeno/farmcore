# Security

FarmCore is early-stage software (0.x). Only the latest release on `main` receives fixes.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this
repository. Do not open a public issue, and do not include real credentials or farm data in a
report. You will get an acknowledgement within a week; fixes are released as soon as they are
ready and credited unless you ask otherwise.

## Access model

Farm reads are private by default, including on localhost and when `ADMIN_TOKEN` is unset.
This access phase uses the installation operator token; member accounts and authenticated
recipient grants are separate work. Do not treat a numeric grant URL as a credential.

| Who | Can |
|---|---|
| Administrator (`ADMIN_TOKEN` holder) | Farm reads and operator actions, including global model-call audit |
| Anonymous visitor | Health, static files and sign-in; no private farm reads or writes |
| Explicit demo visitor | Read only explicitly designated demo farms; no model-call audit |
| Tour visitor (`/tour`) | Same farm permissions as any visitor, with all mutations refused |
| Numeric grantee URL | No authority to read a private farm; operator access required until recipient authentication |

- Set `ADMIN_TOKEN` with `openssl rand -hex 24`. `FARMCORE_PUBLIC=1` requires at least
  32 characters without whitespace and disables interactive API docs. Missing/empty tokens
  never grant operator privileges.
- The operator signs in at `/admin/sign-in`; its cookie contains the token itself
  (HttpOnly, Secure, SameSite=Strict, 12 h). Scripts send `X-Admin-Token`. Rotating the token
  signs browsers out. Never put the token in a URL.
- Public showcases require both `DEMO_MODE=true` and `DEMO_FARM_IDS=[1,2]` (actual IDs of
  intentionally public farms). Seeding demo data alone does not publish it. Use a dedicated
  showcase database; designation publishes that farm's supported read views and exports.
  A forged farm selector or tour cookie cannot publish other farms. Missing designations
  fail closed. Demo mode never grants write/admin access, even without an operator token.
- `/chat/` and shared model-call audit reads remain operator-only, including metadata;
  audit rows have no farm binding. Anonymous demo views never query the global audit.
- Sign-in failures are throttled per client in memory (per process, not distributed).
  Security rests on token entropy, not on the limiter.

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

No per-user accounts or authenticated grant recipients yet; private HTTP access uses the
installation operator token. No
distributed rate limiting, and no independent validation of the agronomic rules. The advice is
decision support; product labels and local regulations take precedence.
