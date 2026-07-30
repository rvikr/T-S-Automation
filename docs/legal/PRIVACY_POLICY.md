# Privacy Policy — TEMPLATE

> **⚠️ TEMPLATE — NOT LEGAL ADVICE.** Adapt with qualified counsel for your
> jurisdiction (GDPR, UK GDPR, CCPA, COPPA as applicable) and your actual
> practices before publishing. Bracketed fields are placeholders.

_Effective date: [DATE]_ · _Controller/Processor: [LEGAL ENTITY NAME]_

## What we process

- **Submitted content.** Text, images, audio, and video that our customers
  submit for moderation. This may incidentally contain personal data of the
  customer's end users — including, in a moderation context, sensitive
  material. We process this content as a **processor** on the customer's
  instructions.
- **Moderation records.** Verdicts, policy citations, model-generated
  descriptions of content, reviewer decisions and rationales, timestamps, and
  correlation IDs.
- **Account and operational data.** Tenant names, hashed API keys, admin actor
  names, request logs (IP address, request ID, latency), and service metrics.

## Why (legal bases)

Providing the moderation service under contract; safety and legal obligations
(including mandatory reporting of child sexual abuse material to [AUTHORITY]);
security of the service (abuse and fraud prevention); and, where applicable,
legitimate interest in improving detection quality. [Counsel: map each purpose
to a GDPR Art. 6 basis; note Art. 10 / special-category handling for
moderation content.]

## Children

The service moderates content from platforms that may serve minors. We do not
knowingly collect children's personal data for our own purposes; content
submitted for moderation is processed on the customer's behalf. Customers
subject to COPPA or the UK Age-Appropriate Design Code remain responsible for
their own compliance. [Counsel: review carefully — this is the highest-risk
paragraph for this product.]

## Retention

Uploaded and quarantined content: purged after [X] days ([90] for quarantine,
which must outlive the human-review SLA). Quarantined content is encrypted at
rest where the deployment enables it. Audit records: [Y] months. Backups:
rotated on a [14]-snapshot schedule.

## Sharing

Subprocessors: [OpenAI (inference), Atlassian (ticketing, if enabled), HOSTING
PROVIDER]. Law enforcement and safety bodies: only as legally required (e.g.
CSAM reporting) or with valid process. We do not sell personal data.

## Security

API keys stored as SHA-256 digests; admin actions attributed to named actors;
tenant-scoped data access; TLS in transit (deployment requirement); optional
encryption at rest for quarantined content; access and rate limiting.

## Your rights

End-user data-subject requests should be directed to the platform (our
customer) that collected the data; we assist our customers in fulfilling them.
Direct inquiries: [PRIVACY CONTACT EMAIL]. [EU/UK representative: NAME,
ADDRESS if required.]

## Changes

We will post updates here with a new effective date and notify customers of
material changes [30] days in advance.
