# Official price adapter contract

Official price collection is adapter-driven. Every adapter selects an already
reviewed HTTPS URL for an existing catalog offering and parses only evidence
returned by `OfficialHttpClient`. An adapter must never perform its own network
request, choose database primary keys, execute SQL, publish a price, call a
search engine, or treat a search-result summary as official evidence.

## Built-in deterministic JSON adapter

The runnable MVP is `tokensql.official.configured:create_adapter`. It reads a
strict JSON configuration from `TOKENSQL_OFFICIAL_ADAPTER_CONFIG` and supports
only an explicitly mapped official JSON response. It does not call Qwen.

Start by copying the schema-only placeholder:

```bash
cp examples/official-adapter.example.json /secure/path/official-adapter.json
export TOKENSQL_OFFICIAL_ADAPTER_CONFIG=/secure/path/official-adapter.json
```

The checked-in example uses the reserved `.invalid` domain and placeholder
catalog values. It is not a real vendor adapter or a real quotation and must not
be used unchanged.

A reviewed configuration has this shape:

```json
{
  "schema_version": 1,
  "source_name": "reviewed-official-json-v1",
  "routes": [
    {
      "provider_slug": "reviewed-provider-slug",
      "api_model_id": "reviewed-api-model-id",
      "region": "global",
      "url": "https://official-host.example/pricing/model.json",
      "records_pointer": "/prices",
      "plan_name": {"literal": "reviewed-plan-name"},
      "currency_code": {"pointer": "/currency"},
      "effective_from_pointer": "/effective_from",
      "components": [
        {
          "metric_code": "reviewed_catalog_metric_code",
          "amount_pointer": "/input_amount",
          "unit_name": "token",
          "unit_quantity": "1000000",
          "conditions": {}
        }
      ],
      "max_records": 100
    }
  ]
}
```

All JSON pointers except `records_pointer` are relative to each selected price
record. `plan_name` and `currency_code` each accept exactly one of `literal` or
`pointer`. A route must identify an offering using the exact provider slug,
region, and at least one of API model ID/provider model name. Component rules use
a reviewed billing metric *code*; Python resolves catalog relationships later.
No LLM sees or returns catalog IDs.

Configuration validation rejects unknown fields, duplicate JSON keys, duplicate
route selectors/URLs, HTTP or credential-bearing URLs, URL query/fragment data,
invalid JSON pointers, duplicate component metrics, invalid tiers, and unbounded
record counts. Runtime parsing additionally requires UTF-8 `application/json`,
a unique configured request URL, an array of object records, finite
non-negative decimal amounts, three-letter currencies, timezone-aware effective
dates, and every configured pointer to exist. Missing fields or page/schema
changes cause review and failure audit rather than guessed values.

Invoke it through the one-shot CLI after separately reviewing the config,
bootstrap catalog IDs, official `DataSource`, and hostname allowlist:

```bash
export TOKENSQL_DATABASE_URL='sqlite+pysqlite:///./var/tokensql.db'
export TOKENSQL_OFFICIAL_ADAPTER_CONFIG=/secure/path/official-adapter.json
uv run tokensql official-refresh \
  --data-source-id <primary-official-source-id> \
  --allowed-domain <exact-official-host> \
  --adapter tokensql.official.configured:create_adapter \
  --limit 20 \
  --timeout 10
```

The command executes once. Each run prioritizes offerings with no verified
official price, then refreshes existing prices from the oldest verification or
collection time; the global limit applies once and each offering appears once.
`OfficialHttpClient` is the only network boundary and still enforces HTTPS, the exact domain allowlist, public destinations, same-host
redirects, content type, timeout, retries, and response-size limits. Adapter
candidates always enter `StagedQuote` as `needs_review`; they are never published
without a separate explicit verification decision and complete catalog links.

## Custom adapters

The CLI also accepts a reviewed adapter object or no-argument factory using a
safe Python import path such as `my_package.vendor_prices:create_adapter`. The
object must satisfy `OfficialPriceAdapter` from
`tokensql.services.official_service`:

```python
from collections.abc import Mapping, Sequence
from typing import Any

from tokensql.db.models import Offering
from tokensql.official.base import FetchResult


class ReviewedVendorAdapter:
    source_name = "reviewed-vendor-official-pricing"

    def supports(self, offering: Offering) -> bool:
        ...  # exact reviewed catalog selectors only

    def url_for(self, offering: Offering) -> str:
        ...  # allowlisted official HTTPS URL only

    def parse(self, result: FetchResult) -> Sequence[Mapping[str, Any]]:
        ...  # parse result.raw_body; never infer missing values or set verified


def create_adapter() -> ReviewedVendorAdapter:
    return ReviewedVendorAdapter()
```

Test custom adapters with recorded fixtures and fake HTTP clients. Cover
unchanged pages, layout/schema changes, empty results, duplicate bodies, 403/429,
regional pages, response-size limits, currency/unit ambiguity, and every special
pricing condition. Tests and CI must not contact live vendor sites.

## Qwen scope

The current `QwenClient` only selects a model from a Python-supplied candidate
list. It has no price-extraction interface. Official JSON prices are therefore
parsed deterministically by the built-in adapter; ambiguous price semantics stay
in human review. This implementation does not claim or perform Qwen semantic
price extraction, and it does not contact the shared Qwen service.
