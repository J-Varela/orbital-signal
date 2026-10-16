# Product Direction

Orbital Signal could become a focused evidence workspace for analysts and
business-development teams tracking space companies. Its useful foundation is
already present: attributable awards, explainable classifications, persistent
identities, and ranked company summaries.

The central workflow would be to discover a company, understand the public
events behind its profile, follow it over time, and receive relevant changes.
This is a proposed direction, not a list of delivered features or a release
commitment.

## 1. Make evidence collection dependable

Move ingestion into scheduled jobs with durable checkpoints, retry diagnostics,
explicit coverage reports, and recovery from interrupted runs. Report date
windows, agencies, page limits reached, source freshness, and failures so users
can distinguish low activity from missing data.

Keep source fetching separate from scoring. Retaining a wider evidence set
would allow relevance-rule changes to recover awards that initial ingestion
discarded. Track rule versions and refresh priority scores deliberately so a
"recent" ranking stays meaningful.

## 2. Make company histories useful

Build a company detail view around a source-linked timeline. Show which records
were merged, why aliases belong together, and where identity is uncertain.
Keep established contractors, institutions, and candidate companies visible
with clear filters and explanations.

Add watchlists and change-based alerts after ingestion is reliable. Useful
alerts could include a new qualifying award, a new agency relationship, or a
new filing. Re-ingesting an old record should not repeatedly alert as a new
event; updates and first observation need distinct semantics.

## 3. Add complementary sources carefully

SBIR/STTR records are a plausible next source for small-business research
activity, phase progression, and technology topics. SEC Form D could add
financing-offering evidence once access, amendments, and issuer identity are
handled explicitly.

Each source should contribute its own event type and interpretation. Contracts,
research grants, and securities offerings should remain distinguishable. A
Form D filing does not establish completed fundraising, and an award amount is
not verified revenue. Cross-source matches need evidence and correction paths.

## 4. Deliver a simple dashboard

A first interface could have three views:

- A ranked activity feed with source, date, relevance, priority, and candidate
  filters, plus explanations and evidence links.
- A company directory with profiles, timelines, identity details, and watchlists.
- An ingestion coverage view showing source freshness, completed/failed runs,
  and known gaps.

Add authentication before exposing a shared hosted service. The current API
assumes a trusted local client and has no access controls. Keep the first
dashboard small enough that users can understand a result and inspect its
evidence without a complex setup flow.

## How to judge progress

Prioritize reliable collection and useful evidence over expanding the number
of sources or scores. Measure coverage gaps, duplicate alerts, incorrect company
merges, classification false positives, and the time it takes a user to verify
a result. Those observations should determine the next investment in the
product.
