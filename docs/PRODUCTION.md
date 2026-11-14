# Production path

What changes between this demo and a live service. Each step keeps the same API.

1. **Auth.** Auth is a seam today: static `X-Agent-Key` per agent (env `AIMO_AGENT_KEYS`) and one `X-Admin-Key`. Replace both with OAuth 2.0 client credentials or mTLS per agent. Mandates bind to the client identity.
2. **Keys.** Key rotation and multi-key JWKS (`retired-*.json` via `python -m aimo.keys rotate`, keeping old quotes verifiable) are already implemented in `aimo/keys.py`. Production moves key storage from local files to a KMS or HSM and rotates on a schedule.
3. **Orders.** Add an ERP order adapter behind `orders.py`. Orders stay `placed` until the ERP confirms, then become `confirmed`. Use an idempotency key per quote.
4. **Storage.** All access goes through `aimo/db.py` (`AIMO_DB`), so SQLite to Postgres is a change in one place plus the per-module table setup. `aimo/audit.py` already signs EdDSA checkpoints (`GET /v1/audit` `checkpoint`, verified via `python -m aimo.audit verify-checkpoint`); production anchors those checkpoints periodically to an external log.
5. **Rate limiting.** Per agent and per customer limits at the gateway. Keep the Valio cache and batching so we never overload their API.
6. **Observability.** Structured logs, metrics and traces with the order id as correlation id. Alert on blocked orders, approval backlog and Valio errors.
7. **Rollout.** Start read-only (search, offers), then quotes, then orders with approval always required. Relax to auto only per customer, after the audit trail has been reviewed.

## Next steps

- Real substitution and allergen data checks with Valio.
- Standing-order drift (`agents/drift.py`): prototyped against seeded history today; would connect to live ERP order history in production.
- Proactive seller (`agents/seller.py`): prototyped against gateway order, audit and seeded history today; would connect to live ERP order history in production.
