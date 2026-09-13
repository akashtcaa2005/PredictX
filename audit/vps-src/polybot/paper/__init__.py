"""Paper trading: a self-contained simulator that shares the live code paths.

The paper engine runs the *same* strategy, broker, order manager, risk engine and
portfolio as live trading. Only two things are swapped out:

* :class:`~polybot.paper.exchange.PaperExchange` — reads real order books from the
  public CLOB endpoints but matches orders internally instead of submitting them;
* :class:`~polybot.paper.account.PaperAccount` — a virtual cash ledger.

No wallet credential is ever loaded into the paper process (see
:func:`polybot.config.settings.scrub_wallet_secrets`), so it has no route to real
funds even if it is misconfigured.
"""
from polybot.paper.account import PaperAccount, PaperBalanceManager
from polybot.paper.exchange import PaperExchange

__all__ = ["PaperAccount", "PaperBalanceManager", "PaperExchange"]
