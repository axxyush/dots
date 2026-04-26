"""Dedicated Fetch.ai agent for executing test-FET payments on Dorado."""

import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    pass

from uagents import Agent, Context

from agents.payment import FAUCET_URL, make_wallet_from_seed, send_tokens
from agents.payment_protocol import PaymentRequest, PaymentResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("payment.agent")

AGENT_SEED = os.environ.get("PAYMENT_AGENT_SEED", "dots-payment-agent-seed-phrase")
USER_WALLET_SEED = os.environ.get("SENSEGRID_USER_PROXY_SEED", "braille-map-demo-user-wallet")

import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

agent = Agent(
    name="payment",
    seed=AGENT_SEED,
    port=8005,
    endpoint=["http://127.0.0.1:8005/submit"]
)

user_wallet = make_wallet_from_seed(USER_WALLET_SEED)


@agent.on_message(model=PaymentRequest)
async def handle_payment_request(ctx: Context, sender: str, msg: PaymentRequest):
    ctx.logger.info("Received PaymentRequest from %s for %d atestfet", sender[:12], msg.amount_atestfet)
    try:
        tx_hash = send_tokens(user_wallet, msg.pay_address, msg.amount_atestfet)
        ctx.logger.info("Payment successful, tx_hash: %s", tx_hash)
        await ctx.send(sender, PaymentResponse(
            session_sender=msg.session_sender,
            success=True,
            tx_hash=tx_hash
        ))
    except Exception as exc:
        ctx.logger.error("Payment failed: %s", exc)
        await ctx.send(sender, PaymentResponse(
            session_sender=msg.session_sender,
            success=False,
            error_msg=str(exc)
        ))

if __name__ == "__main__":
    log.info("Payment Agent starting.")
    log.info("Agent Address: %s", agent.address)
    log.info("Proxy wallet address: %s", user_wallet.address())
    agent.run()
