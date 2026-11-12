# Official public samples

Fetched on 2026-10-31 from the endpoints in `docs/provider-status.md`.
These are reduced responses: unrelated metadata and Google incident update
bodies are removed. Component identifiers/names, feed structure, status fields
and incident lifecycle fields retain the original public values. All six
base samples currently normalise to operational. Tests mutate copies to exercise
outages, maintenance, irrelevant incidents and schema failures, without network.

`xai.json` reduces the public `/api/status` response to Grok and the API product
with its three API components. Its `updated_at` is frozen to the test clock.
The `xai-degraded`, `xai-outage`, `xai-maintenance`, `xai-unknown`, and
`xai-malformed` fixtures are synthetic variants, not observed incidents.
The page bundles confirm `available`, `info`, `disruption` (labelled Degraded),
and `outage`; literal `maintenance` is supported by meaning but was not observed
in the public response or bundle. Unknown values remain unknown.
