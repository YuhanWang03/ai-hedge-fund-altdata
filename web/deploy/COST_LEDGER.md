# Unified usage ledger

The workbench `/api/costs` report includes existing `query_costs` records and new
`usage_events` from `data/query_costs.db`. Never delete this database on deploy.
The schema upgrade is additive. Old amounts are not repriced or duplicated.

## Prices

Use the owner-only **花费 → 价格版本与核对** form to append a price version.
Use the exact model returned in the recent request record, not an assumed alias.
Select USD or CNY for every price version. LLM prices are per million
input/cache-input/output tokens; Tavily is per credit and its model key is
`search`. FD model keys are endpoint names such as
`financial_metrics`. Record the source and applicable plan, effective timestamp
and review deadline. An expired or absent rate yields a pending amount. The old
FD_PRICES/default rate is retained only until an explicit FD version is supplied.

Every event freezes its rate and version at recording time. Adding a price later
does not reprice historical events, including pending entries. DeepSeek official
prices are synchronized automatically as described below; other providers use
configured/manual rates. Unknown usage
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

## Automatic official DeepSeek prices

The web backend checks the fixed official Chinese pricing URL on startup and
every six hours. POST `/api/costs/prices/sync` requests an owner-authenticated
manual check (one-minute throttle). The page shows status and observed rates.
Parsing requires a complete recognized CNY model table and Beijing peak schedule;
failure preserves prior versions. Successful snapshots expire after 24 hours.
Prices apply only from observation time, never retroactively to historical pending
events. Each event selects peak/off-peak by its recorded timestamp and freezes the
chosen rates. Raw official HTML is retained in the price database for auditing,
but excluded from API responses and event snapshots. The API lists the latest
100 versions. Manual DeepSeek rates may be superseded by later official checks.
FD endpoint configuration and manual Tavily package rates remain independent.

## Separate currency totals

## Account-aware billing and reconciliation

New LLM rows preserve both `requested_model` and the provider response `model`.
An exact response price is preferred; if absent, an exact requested-model price
can be used as a visibly labelled estimate. No undocumented global alias is
assumed. Owner-confirmed model mappings have explicit validity dates and a source.
`POST /api/costs/reconcile` only updates pending LLM rows with a valid historical
price and complete usage; the original and recalculated row are retained in
`billing_audit`. It is idempotent and does not reprice settled rows or guess past
Tavily allowance. Events carry three token/cost components, including the
applied peak/off-peak rates.

`POST /api/costs/tavily/sync` reads the official account `/usage` endpoint using
the server key. Only the user-confirmed Researcher/free 1000-credit plan is
automatically accepted. The backend attempts sync every five minutes, with a
one-minute cross-process throttle. Errors are sanitized and surfaced. Quotas
use separate UTC monthly state; each new month requires a fresh snapshot, not
an assumed new allowance. Snapshot age over 24 hours, changed key/plan, missing
credits or pre-snapshot events leave cost pending. Calibrations are audited.
Post-snapshot allocation is atomic across workers: first remaining free credits,
then $0.008 per excess credit. Official sync cannot decrease locally consumed
quota within the same month/key. This conservative estimate can differ from
provider billing due to other applications, overlapping requests and delayed
usage reports; account totals are never added to project spend.

The owner can calibrate current account use via `/api/costs/tavily/calibrate`.
Never reset `tavily_quota` when clearing project `usage_events`/`query_costs`.
Raw manual Tavily prices remain backwards-compatible without an account quota;
such estimates explicitly state that no free allowance was deducted. Once quota
tracking is configured it takes precedence, including month-rollover safeguards.

Each new event freezes `amount` and `currency` with its price snapshot. CNY and
USD totals and provider breakdowns are independent; there is no exchange rate or
combined monetary total. Existing USD entries remain USD; unknown-price events
have no assigned currency. The legacy `cost_usd` field/SQL column remains USD-only
(NULL for CNY), for backwards-compatible consumers. The authoritative multi-
currency amounts are in the event payload and report `currencies`/`amounts`.
