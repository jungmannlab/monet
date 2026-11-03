"""
monet/updates.py
~~~~~~~~~~~~~~~~~

Check whether a newer monet release is available on GitHub.

The GUI's Help ▸ Check for Updates… uses this to tell the user if the installed
build is behind the latest tagged release. It only *reports* — monet does not
self-update; the user downloads the new installer from the Releases page.

Kept dependency-light (``requests`` only, already a core dep via ``monet.io``)
and side-effect-free so it is easy to unit-test by monkeypatching
``requests.get``.
"""

import logging

import monet

logger = logging.getLogger(__name__)

# The canonical repository (mirrors setup_core's SETUP_SCRIPT_URL slug).
GITHUB_REPO = "jungmannlab/monet"
RELEASES_URL = "https://github.com/{}/releases".format(GITHUB_REPO)
LATEST_RELEASE_API = "https://api.github.com/repos/{}/releases/latest".format(
    GITHUB_REPO
)


def _normalize(tag):
    """Strip a leading ``v`` from a release tag (``v0.6.1`` -> ``0.6.1``)."""
    tag = (tag or "").strip()
    return tag[1:] if tag[:1].lower() == "v" else tag


def _is_newer(latest, current):
    """True if version string ``latest`` is strictly newer than ``current``.

    Prefers :mod:`packaging.version` (PEP 440 ordering); falls back to a
    numeric-tuple compare if it is unavailable. Unparseable versions (e.g. the
    ``"unknown"`` of an untagged source tree) are treated as "not newer" so the
    GUI never nags about a non-release build.
    """
    if not latest or not current or current == "unknown":
        return False
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            return False
    except Exception:  # pragma: no cover - packaging is ~always present

        def _tuple(v):
            return tuple(int(p) for p in v.split(".") if p.isdigit())

        try:
            return _tuple(latest) > _tuple(current)
        except Exception:
            return False


def latest_release(timeout=10):
    """Return the latest release's normalized tag (e.g. ``"0.6.1"``).

    Raises on a network/HTTP/parse error — callers that want a verdict dict
    instead should use :func:`check_for_update`.
    """
    import requests

    resp = requests.get(
        LATEST_RELEASE_API,
        timeout=timeout,
        headers={"Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    return _normalize(resp.json().get("tag_name"))


def check_for_update(current=None, timeout=10):
    """Compare the installed version against the latest GitHub release.

    Never raises: a network/offline error is reported in the returned dict so
    the GUI can show a friendly message.

    Returns a dict with:
      ``current``          installed version (``monet.__version__``)
      ``latest``           latest release version, or ``None`` on error
      ``update_available`` True iff ``latest`` is strictly newer than current
      ``url``              the Releases page to download from
      ``error``            a human-readable error string, or ``None``
    """
    current = current or monet.__version__
    result = {
        "current": current,
        "latest": None,
        "update_available": False,
        "url": RELEASES_URL,
        "error": None,
    }
    try:
        latest = latest_release(timeout=timeout)
    except Exception as exc:
        logger.debug("update check failed", exc_info=True)
        result["error"] = str(exc)
        return result
    result["latest"] = latest
    result["update_available"] = _is_newer(latest, current)
    return result
