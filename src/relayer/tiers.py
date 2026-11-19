"""Abstract model tiers.

The four capability rungs from disciplined-project-seed's draft ADR-0020.
Heteronyms and templates name a tier, never a model; the runtime config maps
tiers to the models of whichever agent tool it launches.
"""

TIERS = ("fast", "standard", "strong", "frontier")


def rank(tier: str) -> int:
    """Position of ``tier`` on the ladder; raises ValueError if unknown."""
    return TIERS.index(tier)
