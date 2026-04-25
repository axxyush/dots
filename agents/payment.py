"""Test-FET paywall plumbing for the SenseGrid agent.

Uses the Fetch.ai Dorado testnet so judges can claim free test FET from the
faucet and pay the agent before receiving the final reconstruction.

Multi-user disambiguation: each session quote = base price + a small random
nonce, so the on-chain transaction amount is unique per session and any tx in
the explorer can be traced back to its session by amount alone.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
from dataclasses import dataclass

from cosmpy.aerial.client import LedgerClient, NetworkConfig
from cosmpy.aerial.wallet import LocalWallet
from cosmpy.crypto.keypairs import PrivateKey

log = logging.getLogger("sensegrid.payment")

# 1 FET = 10^18 atestfet on Dorado.
_ATESTFET_PER_FET = 10**18

PRICE_PER_PLAN_FET = float(os.environ.get("SENSEGRID_PRICE_PER_PLAN_FET", "0.5"))
PAYMENT_TIMEOUT_S = int(os.environ.get("SENSEGRID_PAYMENT_TIMEOUT_S", "600"))
# Dorado explorer/faucet — judges paste an address here to claim test FET.
FAUCET_URL = "https://companion.fetch.ai/dorado-1/accounts"

_LEDGER: LedgerClient | None = None


def ledger() -> LedgerClient:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = LedgerClient(NetworkConfig.fetchai_dorado_testnet())
    return _LEDGER


def fet_to_atestfet(fet: float) -> int:
    return int(round(fet * _ATESTFET_PER_FET))


def atestfet_to_fet_str(atestfet: int) -> str:
    return f"{atestfet / _ATESTFET_PER_FET:.6f}".rstrip("0").rstrip(".") or "0"


@dataclass
class Quote:
    n_plans: int
    base_atestfet: int
    nonce_atestfet: int  # < 10^6 atestfet (a vanishing fraction of 0.5 FET)

    @property
    def total_atestfet(self) -> int:
        return self.base_atestfet + self.nonce_atestfet

    @property
    def total_fet_str(self) -> str:
        return atestfet_to_fet_str(self.total_atestfet)


def quote_for(n_plans: int) -> Quote:
    """Per-session quote with a unique atestfet amount (base + nonce)."""
    base = fet_to_atestfet(PRICE_PER_PLAN_FET * max(1, n_plans))
    nonce = random.randint(1, 999_999)
    return Quote(n_plans=n_plans, base_atestfet=base, nonce_atestfet=nonce)


def address_balance_atestfet(address: str) -> int:
    return int(ledger().query_bank_balance(address))


def payment_received(
    address: str,
    start_balance_atestfet: int,
    expected_delta_atestfet: int,
) -> tuple[bool, int]:
    """True iff balance has increased by at least `expected_delta` since
    session start. Returns (received, current_balance)."""
    current = address_balance_atestfet(address)
    received = (current - start_balance_atestfet) >= expected_delta_atestfet
    return received, current


# ── Autonomous payment (agent pays from a user-proxy wallet) ─────────────────


# Small buffer above the quoted amount so the user-proxy wallet can always
# cover gas. Dorado fees are ~5_000 atestfet per simple send; 10^15 atestfet
# (= 0.001 FET) is overkill but trivial vs a 0.5 FET test transfer.
GAS_BUFFER_ATESTFET = 10**15


def make_wallet_from_seed(seed: str) -> LocalWallet:
    """Deterministic wallet from a string seed. Same wallet every run, so
    judges fund it from the Dorado faucet once and the demo keeps working."""
    key = PrivateKey(hashlib.sha256(seed.encode()).digest())
    return LocalWallet(key)


def send_tokens(from_wallet: LocalWallet, to_address: str, atestfet: int) -> str:
    """Sign + broadcast a transfer on Dorado, blocking until the tx lands.
    Returns the tx hash. Raises on failure (insufficient funds, network)."""
    tx = ledger().send_tokens(
        destination=to_address,
        amount=atestfet,
        denom="atestfet",
        sender=from_wallet,
    )
    tx.wait_to_complete()
    return tx.tx_hash
