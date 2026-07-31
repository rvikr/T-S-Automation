# Sentinel Policy Corpus

This corpus is a compact taxonomy for demo grounding. It is mapped from the four Roblox Community Standards pillars without copying the standards' full text. Source link: https://about.roblox.com/community-standards

The machine-readable source of truth is `POLICY_CLAUSES` in `sentinel/tools/policy_retrieval.py`; this document is the human-readable mirror. The semantic index is built from the same source with `python -m sentinel.tools.policy_index`.

## Safety

- `SAF-CE-001` Child Exploitation: Tier 1. Synthetic stand-ins only. Always hash-match, quarantine, and route to human review.
- `SAF-TVE-001` Terrorism & Violent Extremism: Tier 1. Synthetic stand-ins only. Always hash-match, quarantine, and route to human review.
- `SAF-SSI-001` Suicide/Self-Injury: Tier 2. Context-sensitive references should escalate when unclear.
- `SAF-HAR-001` Harassment & Discrimination: Tier 2. Abusive or identity-targeted conduct is disallowed; unclear social context escalates.
- `SAF-OFF-001` Harmful Off-Platform Behavior: Tier 2. Unsafe off-platform behavior requires strict review.

## Civility

- `CIV-VCG-001` Violent Content & Gore: Tier 3. Clear synthetic labels are rejected.
- `CIV-RSX-001` Romantic/Sexual Content: Tier 3. Clear synthetic labels are rejected.
- `CIV-IRG-001` Illegal & Regulated Goods: Tier 3. Promotion or facilitation is rejected.
- `CIV-PRO-001` Profanity: Tier 3. Explicit user-facing profanity is rejected.
- `CIV-SEN-001` Sensitive Events: Tier 2. Context-sensitive cases escalate.
- `CIV-POL-001` Political Content: Tier 2. Campaigning or persuasion labels escalate when unclear.

## Integrity

- `INT-SCAM-001` Cheating & Scams: Tier 3. Clear synthetic labels are rejected.
- `INT-SPAM-001` Spam: Tier 3. Repeated disruptive promotion is rejected.
- `INT-IP-001` IP Violations: Tier 3. Unauthorized protected-IP labels are rejected.
- `INT-ADV-001` Advertising: Tier 3. Unapproved solicitation is rejected.

## Security

- `SEC-PII-001` Sharing Personal Info: Tier 3. Personal information labels are rejected.
- `SEC-OPD-001` Off-Platform Directing: Tier 2. Unclear intent escalates.
- `SEC-MIS-001` System Misuse: Tier 3. Abuse of systems or security boundaries is rejected.

## Tier-1 Detection Seam

Tier-1 categories (Child Exploitation; Terrorism & Violent Extremism) are never adjudicated by AI.
All assets go directly to quarantine and a human review ticket. Detection happens in
`sentinel/tools/hash_match.py` via `known_hash_match()`. The upgrade path for that function is:

| Level | Approach | Dependencies |
|---|---|---|
| **Short-term** | Add SHA-256 hex digests to `sentinel/data/known_hashes.txt` (one per line, `#` = comment) | None — works today |
| **Medium-term** | Add PDQ perceptual hash support (`pip install pdqhash`) to catch re-encoded near-duplicates; store with `pdq:` prefix in the hash list | Open-source, no external service |
| **Long-term** | NCMEC ESP registration + hash-check API, or Azure Content Safety (PhotoDNA) for image matching | NCMEC: legal/compliance registration; Azure: subscription |

The function signature of `known_hash_match(asset_path)` never changes — only its body is updated at each level.
See the docstring in `sentinel/tools/hash_match.py` for full implementation notes.
