# Unified usage ledger

The workbench `/api/costs` report includes existing `query_costs` records and new
`usage_events` from `data/query_costs.db`. Never delete this database on deploy.
The schema upgrade is additive. Old amounts are not repriced or duplicated.

## Prices

Use the owner-only **花费 → 价格版本与核对** form to append a price version.
Use the exact model returned in the recent request record, not an assumed alias.
LLM prices are USD per million input/cache-input/output tokens; Tavily is USD per
credit and its model key is `search`. FD model keys are endpoint names such as
`financial_metrics`. Record the source and applicable plan, effective timestamp
and review deadline. An expired or absent rate yields a pending amount. The old
FD_PRICES/default rate is retained only until an explicit FD version is supplied.

Every event freezes its rate and version at recording time. Adding a price later
does not reprice historical events, including pending entries. No automatic
website scraping or automatic price changes are performed. Check the official
pricing links in the UI, and add a new version when prices change. Unknown usage
and missing cached-input details are never treated as zero-dollar consumption.

Tavily search requests ask for `include_usage=true`; credits come from the
response, not guessed depth. Monetary values are plan-rate estimates, not cash
charges. Free credits, subscriptions, gifts and refunds require bill reconciliation.
Only newly instrumented calls are recorded; historical LLM/search usage cannot
be reconstructed. SDK-internal failed retries without usage remain unobservable.

The LangChain and Tavily adapters are explicit and do not require an active
observability trace. Agent HTTP completions and research translation calls also
record returned usage before application-level parsing. Embeddings and streaming
LLM calls are not included. Do not sum trace cost estimates into this ledger.
Current provider adapters support invoke/ainvoke and search; future provider
entry points must use the adapters or explicitly call the ledger once.

## Official balance

The owner-only `/api/costs/deepseek-balance` reads the fixed official URL using
server-side DEEPSEEK_API_KEY, with a 10-second timeout and 60-second cache. It
returns only balance fields and sanitized errors. It is account-level, separate
from project estimates. It never derives costs from balance changes.

## Deployment

Rebuild ai-workbench and restart the web service. Restart any running bot,
scheduler and streamer processes to activate their new provider imports.
The database belongs to the service account; all those processes must share
the same checkout/data directory to contribute to a single project ledger.
No API keys, prompts, completions or search query text are stored in usage rows.

## Origin channel

Events share one ledger and carry `channel=web|telegram|background|unknown`.
HTTP middleware sets web; authorized Telegram handlers set telegram. Context is
isolated per request and explicitly copied into project worker pools and web job
threads. Existing entries without this field are shown as unknown, not inferred.
Independent scheduler/streamer processes may set `USAGE_CHANNEL=background` in
their service environment. Do not set a global web/telegram value on shared
processes; entry-point contexts take priority over this fallback.
