"""Compatibility exports for the provider capability catalogue."""
from ..provider_capabilities import (
    ProviderCapability, CAPABILITIES, capability_for, capability_can_certify,
)

__all__ = [
    "ProviderCapability", "CAPABILITIES", "capability_for",
    "capability_can_certify",
]
