"""One login held in two places (cc-swap fork).

A refresh token is one-time use: the first holder to refresh it gets a new
one, and every other copy is dead from that moment. So two places that hold
the same refresh token cannot both stay logged in — two slots (one slot's
backup was overwritten with another's), a slot and another slot's ``cswap
run`` profile, or a slot that is not the live account and the live login.

cc-swap does not refresh such a slot until one of the two is re-logged
(``cc-swap login N``): the consume gate refuses with :data:`SHARED_LOGIN`,
the active fetch path the same, so nothing cc-swap does decides which copy
dies. ``cc-swap doctor`` names the places. A slot's own copies are not
sharing: its backup and the live login while it is the live account, its
backup and its own ``cswap run`` profile (both are the slot, kept in step
by design).

Only fingerprints (``oauth.credential_fingerprint``: a hash of the refresh
token) are compared or logged — never a token.
"""

from __future__ import annotations

from pathlib import Path

from claude_swap import oauth

#: The consume gate's refusal kind (``RefreshOutcome.error`` / usage error).
SHARED_LOGIN = "shared-login"

#: The place label of the live login (``switcher.shared_login_places``).
LIVE_LOGIN = "the live login"

#: The remedy every surface shows next to the kind.
NOTE = (
    "this login's refresh token is also held elsewhere — cc-swap does not "
    "refresh it until one of them is re-logged (cc-swap doctor names them)"
)


def refresh_fingerprint(credentials: str | None) -> str | None:
    """The refresh-token fingerprint, or None when ``credentials`` carry no
    refresh token (an API key or setup-token is not a one-time-use login)."""
    fp = oauth.credential_fingerprint(credentials or "")
    return fp if fp and fp.startswith("sha256:") else None


def session_profiles(backup_dir: Path) -> list[tuple[str, Path]]:
    """Every ``cswap run`` profile under ``backup_dir`` as ``(slot number it
    was made for, path)``, by directory name (``<num>-<email slug>``)."""
    from claude_swap.session import _PROFILE_DIRNAME, SESSIONS_DIRNAME

    root = Path(backup_dir) / SESSIONS_DIRNAME
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    out: list[tuple[str, Path]] = []
    for path in entries:
        if not _PROFILE_DIRNAME.match(path.name):
            continue
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        out.append((path.name.split("-", 1)[0], path))
    return out


def profile_label(number: str) -> str:
    return f"#{number}'s cswap run profile"


def fix(numbers: list[str]) -> str:
    """The one remedy: a fresh login for one of the slots involved."""
    shown = list(dict.fromkeys(n for n in numbers if n))
    if not shown:
        return "re-login the account: cc-swap login N"
    first = f"cc-swap login {shown[0]}"
    rest = "".join(f" or cc-swap login {n}" for n in shown[1:])
    return f"re-login one of them: {first}{rest}"
