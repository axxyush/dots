"""Helper for sending a uAgent message from outside the uAgents runtime.

Used by the mock backend's `/trigger/{room_id}` endpoint and by `test_pipeline.py`
to hand off a `SpatialProcessingRequest` to Agent 1 without requiring agents
to be registered with the Almanac / Agentverse.

The envelope is built and signed the same way `uagents.Context.send` does it,
then POSTed directly to the agent's local `/submit` endpoint.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from typing import Optional

import requests
from uagents import Model

try:  # uagents >= 0.22 splits crypto/envelope across packages
    from uagents_core.crypto import Identity  # type: ignore
except Exception:  # pragma: no cover - fallback for older layouts
    from uagents.crypto import Identity  # type: ignore

try:
    from uagents_core.envelope import Envelope  # type: ignore
except Exception:  # pragma: no cover
    from uagents.envelope import Envelope  # type: ignore

from schemas import (
    SpatialProcessingRequest,
    MapGenerationRequest,
    NarrationRequest,
    FloorPlanAnalysisRequest,
    RecommendationsRequest,
    address_from_seed,
)

DEFAULT_SENDER_SEED = os.getenv(
    "TRIGGER_SENDER_SEED", "braillemap_backend_trigger_seed_v1"
)


def _schema_digest(message: Model) -> str:
    """Delegate to uagents.Model so the digest matches what the receiver computes."""
    return type(message).build_schema_digest(message)


def send_to_agent(
    endpoint_url: str,
    target_address: str,
    message: Model,
    sender_seed: Optional[str] = None,
    timeout: float = 10.0,
) -> requests.Response:
    """POST a signed envelope carrying `message` to `endpoint_url`."""
    identity = Identity.from_seed(sender_seed or DEFAULT_SENDER_SEED, 0)
    payload_b64 = base64.b64encode(message.model_dump_json().encode()).decode()

    env = Envelope(
        version=1,
        sender=identity.address,
        target=target_address,
        session=uuid.uuid4(),
        schema_digest=_schema_digest(message),
        protocol_digest=None,
        payload=payload_b64,
        expires=int(time.time()) + 30,
    )
    env.sign(identity)

    resp = requests.post(
        endpoint_url,
        data=env.model_dump_json(),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


def trigger_spatial_pipeline(room_id: str) -> None:
    """Send a SpatialProcessingRequest to Agent 1 using only env-configured values."""
    seed_1 = os.getenv("AGENT_SEED_1")
    if not seed_1:
        raise RuntimeError("AGENT_SEED_1 not set in environment")
    port_1 = int(os.getenv("AGENT_PORT_1", "8001"))
    target_address = address_from_seed(seed_1)
    endpoint_url = f"http://localhost:{port_1}/submit"
    send_to_agent(endpoint_url, target_address, SpatialProcessingRequest(room_id=room_id))


def trigger_map_and_narration(room_id: str) -> None:
    """Send MapGenerationRequest + NarrationRequest directly to Agents 3 & 4.

    Used by the floor plan pipeline to skip the scan-specific Agents 1 & 2.
    """
    seed_3 = os.getenv("AGENT_SEED_3")
    seed_4 = os.getenv("AGENT_SEED_4")
    if not seed_3 or not seed_4:
        raise RuntimeError("AGENT_SEED_3 and AGENT_SEED_4 must be set in environment")

    port_3 = int(os.getenv("AGENT_PORT_3", "8003"))
    port_4 = int(os.getenv("AGENT_PORT_4", "8004"))

    addr_3 = address_from_seed(seed_3)
    addr_4 = address_from_seed(seed_4)

    send_to_agent(
        f"http://localhost:{port_3}/submit",
        addr_3,
        MapGenerationRequest(room_id=room_id),
    )
    send_to_agent(
        f"http://localhost:{port_4}/submit",
        addr_4,
        NarrationRequest(room_id=room_id),
    )


def trigger_floorplan_pipeline(room_id: str) -> None:
    """Send a FloorPlanAnalysisRequest to Agent 5."""
    seed_5 = os.getenv("AGENT_SEED_5")
    if not seed_5:
        raise RuntimeError("AGENT_SEED_5 not set in environment")
    port_5 = int(os.getenv("AGENT_PORT_5", "8005"))
    target_address = address_from_seed(seed_5)
    endpoint_url = f"http://localhost:{port_5}/submit"
    send_to_agent(
        endpoint_url,
        target_address,
        FloorPlanAnalysisRequest(room_id=room_id),
    )


def trigger_recommendations(room_id: str) -> None:
    """Send a RecommendationsRequest to Agent 6 (ADA recommendations)."""
    seed_6 = os.getenv("AGENT_SEED_6")
    if not seed_6:
        raise RuntimeError("AGENT_SEED_6 not set in environment")
    port_6 = int(os.getenv("AGENT_PORT_6", "8006"))
    target_address = address_from_seed(seed_6)
    endpoint_url = f"http://localhost:{port_6}/submit"
    send_to_agent(
        endpoint_url,
        target_address,
        RecommendationsRequest(room_id=room_id),
    )


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) != 2:
        print("usage: python trigger.py <room_id>")
        sys.exit(1)
    trigger_spatial_pipeline(sys.argv[1])
    print(f"Trigger sent for room {sys.argv[1]}")
