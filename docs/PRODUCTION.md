# Production path

What changes between this demo and a live service. Each step keeps the same API.

1. **Auth.** Replace seeded `X-Agent-Key` with OAuth 2.0 client credentials or mTLS per agent. Mandates bind to the client identity.
2. **Keys.** Move the Ed25519 signing key to a KMS or HSM. Rotate on a schedule. JWKS publishes old and new `kid` during the overlap so old quotes still verify.
3. **Orders.** Add an ERP order adapter behind `orders.py`. Orders stay `placed` until the ERP confirms, then become `confirmed`. Use an idempotency key per quote.
4. **Storage.** SQLite to Postgres. Audit chain head anchored periodically to an external store.
5. **Rate limiting.** Per agent and per customer limits at the gateway. Keep the Valio cache and batching so we never overload their API.
6. **Observability.** Structured logs, metrics and traces with the order id as correlation id. Alert on blocked orders, approval backlog and Valio errors.
7. **Rollout.** Start read-only (search, offers), then quotes, then orders with approval always required. Relax to auto only per customer, after the audit trail has been reviewed.

## Next steps

- Real substitution and allergen data checks with Valio.
- Standing order drift detection on live order history.
- Seller-side agent for proactive offers.
