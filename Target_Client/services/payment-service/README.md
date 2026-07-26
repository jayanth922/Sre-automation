# payment-service

A downstream dependency of `checkout-service`, added to create a realistic
**dependency chain** (checkout → payment). When this provider degrades or is
taken down (`provider_down`), checkout's error rate spikes even though checkout
itself is healthy — surfacing the *downstream dependency failure* incident class
for the agent to diagnose (and correctly attribute to payment, not checkout).

- Port `8004`; endpoint `POST /charge`.
- Metrics: `http_requests_total`, `http_errors_total`, `payment_failures_total`,
  `payment_provider_up` (0 during an outage).
- Chaos: `POST /admin/config` with `error_rate`, `slow_rate`, `provider_down`.

Checkout calls this service only when `PAYMENT_URL` is set (backward compatible;
without it, checkout keeps simulating payments internally).
