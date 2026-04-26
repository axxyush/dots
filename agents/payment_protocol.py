"""Protocol for inter-agent test FET payments."""

from uagents import Model


class PaymentRequest(Model):
    """Sent by SenseGrid to PaymentAgent to request a transfer."""
    session_sender: str
    amount_atestfet: int
    pay_address: str


class PaymentResponse(Model):
    """Sent by PaymentAgent back to SenseGrid with the result."""
    session_sender: str
    success: bool
    tx_hash: str = ""
    error_msg: str = ""
