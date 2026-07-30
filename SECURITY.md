# Security

## Reporting a vulnerability

Please open a private security advisory on the repository rather than a public
issue. Include reproduction steps and the commit you tested against.

## Deployment assumptions

Sentinel is a reference implementation. Several controls that a production
content-moderation service needs are **deliberately out of scope of the
application** and assumed to be provided by the surrounding infrastructure. If
you deploy this, you own these:

- **TLS termination.** The app speaks plain HTTP; run it behind a reverse proxy.
- **DDoS protection and cross-worker rate limiting.** The API has an in-process
  per-client-IP limiter (defaults: 120 req/min, 30 req/min on `/admin/*` to slow
  admin-token brute force), but it is per-process memory only: it does not
  coordinate across workers or nodes and is not DDoS protection. It also keys on
  the direct peer address and deliberately ignores `X-Forwarded-For` (trivially
  spoofable), so behind a reverse proxy the per-client limiting must happen at
  the proxy.
- **CORS.** No CORS middleware is configured; the API is intended for
  server-to-server use, not direct browser access.
- **Secrets management.** Credentials are read from the environment (`.env`
  locally). Use a real secrets manager in production; do not bake keys into an
  image.
- **Replication and offsite backup storage.** State lives in a single-node
  SQLite file and a local ChromaDB directory; neither is replicated. A backup
  tool ships (`python -m sentinel.tools.backup`, online-backup API + rotation,
  runnable as a compose sidecar), but the operator owns scheduling it and
  copying snapshots somewhere off the host. ChromaDB is intentionally not
  backed up — both indexes are derived data, rebuildable from SQLite and the
  policy corpus.

## What the application does handle

- **API keys** are 256-bit random tokens (`sent_<env>_<token>`), stored only as
  SHA-256 digests and compared with `hmac.compare_digest`. A plaintext key is
  returned exactly once, at creation. Salting is intentionally omitted: for a
  high-entropy random secret it adds nothing against brute force, unlike for
  passwords.
- **Admin routes fail closed.** If no admin token is configured, key
  management returns 503 rather than running unauthenticated.
- **Key scopes, expiry, and rotation.** Keys carry scopes (`moderate`, `logs`)
  enforced per route — a `logs`-only key cannot submit cases. `expires_in_days`
  is checked at authentication and fails closed on an unparseable timestamp.
  `POST /admin/api-keys/{id}/rotate` revokes and re-mints in one step, carrying
  scopes over unchanged so rotation never silently widens access.
- **Attributable admin actions.** `SENTINEL_ADMIN_TOKENS` ("name:token" pairs)
  identifies the acting admin; mints record `created_by` and every admin action
  is logged under that name.
- **Encryption at rest for quarantine.** With `SENTINEL_ENCRYPTION_KEY` set,
  assets are Fernet-encrypted on entry to quarantine and the plaintext source
  is removed; a malformed key raises rather than silently storing plaintext.
- **Tenant isolation** on moderation logs is enforced in the query layer and
  covered by tests.
- **Upload ceiling.** Payloads above `SENTINEL_MAX_UPLOAD_BYTES` (25 MB default)
  are rejected with 413 before anything is written to disk.
- **SQL injection.** All queries are parameterized.
- **Path traversal.** Upload and quarantine filenames are normalized to a
  basename before use.
- **In-process rate limiting.** Per-client-IP fixed-window limits on all API
  routes, with a stricter bucket on `/admin/*` (see deployment assumptions
  above for its limits).
- **Webhook SSRF guard.** Caller-supplied `callback_url` targets are refused
  unless their host is on the operator's `SENTINEL_WEBHOOK_ALLOWED_HOSTS`
  allowlist; redirects are never followed; deliveries can be HMAC-signed via
  `SENTINEL_WEBHOOK_SECRET`.
- **UI cost controls.** The Streamlit tab that triggers paid model calls can be
  password-gated (`SENTINEL_UI_PASSWORD`, constant-time comparison) and is
  capped per session (`SENTINEL_UI_MAX_LIVE_RUNS`, default 25; malformed
  values fall back to the default, never to unlimited).
- **Custom policy validation.** `SENTINEL_POLICY_FILE` taxonomies are strictly
  validated and the service refuses to start on a malformed file — enforcing
  under a policy other than the one the operator configured is treated as
  worse than not starting.

## Known limitations

These are honest gaps, not oversights — they are called out so nobody mistakes
the reference implementation for a hardened one:

- **`hash_match.py` is a stand-in.** It matches on case metadata labels, not a
  real perceptual-hash corpus. It is an integration seam for PhotoDNA/PDQ, and
  provides **no actual known-content detection** as written.
- **Coarse scopes; no true RBAC.** Scopes are per-route grants
  (`moderate`, `logs`), not roles: any admin token can do anything on
  `/admin/*`, and there is no per-tenant admin delegation. Named tokens give
  attribution, not authorization boundaries.
- **Retention and backups require scheduling.** Both tools ship and both run as
  compose sidecars under the `ops` profile, but a bare `docker compose up` does
  not start them — the operator must opt in.
- **Encryption at rest covers quarantine only.** Uploads stay readable while
  the pipeline analyses them, and audit rationales — which contain
  model-generated descriptions of flagged content — live unencrypted in SQLite,
  where field-level encryption would break querying.
- **The injection screen is a first-line filter, not a defense.** Normalization
  (NFKC, zero-width stripping, leetspeak folding) closes the cheap evasions,
  but semantic rephrasings and non-English injection pass it; the model's own
  refusal behavior and the deterministic rails downstream are the real
  backstop.

## Handling of illegal content

No real illegal content exists anywhere in this repository. Tier-1 fixtures are
clearly-labeled text stand-ins used only to verify routing. Tier-1 verdicts are
never adjudicated automatically: they quarantine the asset and open a human
review ticket, by deterministic code the agents cannot reach.
