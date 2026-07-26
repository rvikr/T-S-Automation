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
- **Rate limiting and DDoS protection.** There is no in-process rate limiter.
  A single static admin token with unlimited attempts is brute-forceable without
  a gateway in front of it.
- **CORS.** No CORS middleware is configured; the API is intended for
  server-to-server use, not direct browser access.
- **Secrets management.** Credentials are read from the environment (`.env`
  locally). Use a real secrets manager in production; do not bake keys into an
  image.
- **Backups and replication.** State lives in a single-node SQLite file and a
  local ChromaDB directory. Neither is replicated.

## What the application does handle

- **API keys** are 256-bit random tokens (`sent_<env>_<token>`), stored only as
  SHA-256 digests and compared with `hmac.compare_digest`. A plaintext key is
  returned exactly once, at creation. Salting is intentionally omitted: for a
  high-entropy random secret it adds nothing against brute force, unlike for
  passwords.
- **Admin routes fail closed.** If `SENTINEL_ADMIN_TOKEN` is unset, key
  management returns 503 rather than running unauthenticated.
- **Tenant isolation** on moderation logs is enforced in the query layer and
  covered by tests.
- **Upload ceiling.** Payloads above `SENTINEL_MAX_UPLOAD_BYTES` (25 MB default)
  are rejected with 413 before anything is written to disk.
- **SQL injection.** All queries are parameterized.
- **Path traversal.** Upload and quarantine filenames are normalized to a
  basename before use.

## Known limitations

These are honest gaps, not oversights — they are called out so nobody mistakes
the reference implementation for a hardened one:

- **`hash_match.py` is a stand-in.** It matches on case metadata labels, not a
  real perceptual-hash corpus. It is an integration seam for PhotoDNA/PDQ, and
  provides **no actual known-content detection** as written.
- **No key expiry, scopes, or rotation.** `created_at` and `last_used_at` are
  recorded but nothing acts on them.
- **No data retention policy.** Audit rationales contain model-generated
  descriptions of flagged content, and uploaded assets persist indefinitely with
  no TTL, purge job, or encryption at rest.
- **No RBAC.** A single shared admin token means admin actions are not
  attributable to an individual.

## Handling of illegal content

No real illegal content exists anywhere in this repository. Tier-1 fixtures are
clearly-labeled text stand-ins used only to verify routing. Tier-1 verdicts are
never adjudicated automatically: they quarantine the asset and open a human
review ticket, by deterministic code the agents cannot reach.
