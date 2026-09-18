"""Identity voting for harvested target strategies.

Adapters resolve each strategy to an opaque element identity. This module
decides agreement, weak matches, and conflicts without a browser library.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pigeonhole.surface.base import SurfaceResolutionError


@dataclass(frozen=True)
class IdentityVote:
    identity: str
    winners: list[str]
    agreement: int
    weak: bool
    identities: dict[str, str] = field(default_factory=dict)


def vote_identities(found: list[tuple[str, str]]) -> IdentityVote:
    """Vote on (strategy_kind, element_identity) pairs.

    All strategies must land on one identity. Distinct identities are a
    locator conflict; an empty list is unresolved.
    """
    if not found:
        raise SurfaceResolutionError("no strategies resolved")
    groups: dict[str, list[str]] = {}
    identities: dict[str, str] = {}
    for kind, identity in found:
        groups.setdefault(identity, []).append(kind)
        identities[kind] = identity
    if len(groups) > 1:
        detail = {identity: kinds for identity, kinds in groups.items()}
        raise SurfaceResolutionError(f"locator conflict: {detail}", conflict=True)
    identity, winners = next(iter(groups.items()))
    return IdentityVote(
        identity=identity,
        winners=list(winners),
        agreement=len(winners),
        weak=len(winners) < 2,
        identities=identities,
    )
