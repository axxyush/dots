"""Shared message models and address helper for the BrailleMap agent pipeline.

Messages carry only a `room_id`. The full payload (scan data, layout, photos)
lives in the backend — agents fetch by id to avoid bloated uAgent envelopes.
"""

from __future__ import annotations

from typing import Optional

from uagents import Model
from uagents.crypto import Identity


class SpatialProcessingRequest(Model):
    room_id: str


class EnrichmentRequest(Model):
    room_id: str


class MapGenerationRequest(Model):
    room_id: str


class NarrationRequest(Model):
    room_id: str


class FloorPlanAnalysisRequest(Model):
    room_id: str


class RecommendationsRequest(Model):
    room_id: str


class AgentStatusUpdate(Model):
    room_id: str
    agent_name: str
    status: str
    timestamp: str
    detail: Optional[str] = None


def address_from_seed(seed: str) -> str:
    """Deterministic uAgent address from a seed phrase.

    Lets each agent compute the address of the next agent in the pipeline
    without having to run it first and copy-paste the address.
    """
    if not seed:
        raise ValueError("Agent seed is empty — set AGENT_SEED_* in your .env")
    return Identity.from_seed(seed, 0).address
