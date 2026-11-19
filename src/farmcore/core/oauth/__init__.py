"""OAuth 2.1 for MCP: this installation authorizes an agent, on its farm owner's consent, to call
the MCP endpoint (`core/mcp_http.py`) with a short-lived, audience-bound token.

- `service.py`: authorization requests, codes, connections and their rotating tokens (Postgres).
- `routes.py`: the web app's side (the issuer is `PUBLIC_BASE_URL`): server metadata (RFC 8414),
  the authorize endpoint and owner consent page, and the token and revocation endpoints (the MCP
  SDK's handlers over `service`). The MCP listener serves the resource's metadata (RFC 9728).

Off unless `MCP_OAUTH_ENABLED`. Clients are registered by the operator (`MCP_OAUTH_CLIENTS`).
"""
