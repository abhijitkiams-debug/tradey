"""Groww live adapter — intentionally not implemented.

PRD §9 / §18 Q3 require engineering to verify, against the account actually provisioned:
  * API access is activated and which order workflows the account supports;
  * cash-equity order types (MARKET / LIMIT / SL / SL-M) and product codes (intraday vs delivery);
  * whether a client order reference can be attached and later queried (needed for
    timeout reconciliation — the whole duplicate-protection design depends on it);
  * order modification / cancellation semantics and partial-fill reporting;
  * position, holdings and funds endpoints; rate limits; fees.

Until that is done, constructing this adapter raises, so nothing can be wired to a live
account by accident. Implement it against ``broker.base.BrokerAdapter`` and run the same
test-suite used for ``PaperBroker`` (tests/test_execution.py) against a sandbox first.
Credentials must come from a secrets manager at runtime, never from prompts, code or logs.
"""

from __future__ import annotations


class GrowwBroker:
    name = "groww"

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "Groww adapter is pending API verification (PRD §9, §18 Q3). Use PaperBroker."
        )
