"""Plan weight per account: 20x=4, 5x=1, unknown=1 (spec §8 plan detection)."""

from __future__ import annotations

from claude_swap import oauth

WEIGHT_20X = 4
WEIGHT_5X = 1
DEFAULT_WEIGHT = 1


def _weight_for_label(label: str) -> int | None:
    text = label.strip().lower()
    if "20x" in text:
        return WEIGHT_20X
    if "5x" in text:
        return WEIGHT_5X
    return None


def parse_plan_override(value: str | None) -> dict[str, int]:
    """``"email:20x,email:5x"`` → ``{email_lower: weight}``; bad items skipped."""
    out: dict[str, int] = {}
    if not value:
        return out
    for part in value.split(","):
        email, sep, label = part.strip().rpartition(":")
        if not sep or not email.strip():
            continue
        weight = _weight_for_label(label)
        if weight is not None:
            out[email.strip().lower()] = weight
    return out


def plan_weight(
    rate_limit_tier: str | None, email: str, override: str | None
) -> int:
    """Override first, then the stored ``rateLimitTier``, else 1."""
    forced = parse_plan_override(override).get((email or "").strip().lower())
    if forced is not None:
        return forced
    if isinstance(rate_limit_tier, str):
        weight = _weight_for_label(rate_limit_tier)
        if weight is not None:
            return weight
    return DEFAULT_WEIGHT


def plan_name(
    rate_limit_tier: str | None, email: str, override: str | None
) -> str | None:
    """``20x`` / ``5x`` as :func:`plan_weight` reads it (override first, then
    the stored ``rateLimitTier``), or None when neither says: unlike the
    weight, an unknown plan is not a 5x one."""
    forced = parse_plan_override(override).get((email or "").strip().lower())
    weight = forced
    if weight is None and isinstance(rate_limit_tier, str):
        weight = _weight_for_label(rate_limit_tier)
    if weight == WEIGHT_20X:
        return "20x"
    if weight == WEIGHT_5X:
        return "5x"
    return None


def plan_label(rate_limit_tier: str | None) -> str | None:
    """A stored ``rateLimitTier`` as the TUI's plan label: ``20x``, ``5x``,
    ``team``, or None when it says none of those. Never the raw string."""
    if not isinstance(rate_limit_tier, str):
        return None
    text = rate_limit_tier.strip().lower()
    if "20x" in text:
        return "20x"
    if "5x" in text:
        return "5x"
    if "team" in text or "enterprise" in text:
        return "team"
    return None


def rate_limit_tier_from_credentials(credentials: str | None) -> str | None:
    """``claudeAiOauth.rateLimitTier`` from a stored credential blob, or None.

    Reads one string field; never returns or logs anything else from the blob.
    """
    if not credentials:
        return None
    try:
        data = oauth.extract_oauth_data(credentials)
    except (AttributeError, TypeError, ValueError):
        return None
    tier = data.get("rateLimitTier") if isinstance(data, dict) else None
    return tier if isinstance(tier, str) and tier else None
