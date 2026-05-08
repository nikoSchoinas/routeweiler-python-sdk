"""ManifestRecoveryStrategy — split-URL recovery driven by service-shape manifests."""

from __future__ import annotations

import fnmatch
import logging
import urllib.parse
from typing import TYPE_CHECKING

import httpx

from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.credentials.recovery import RecoveryOutcome
from routeweiler.credentials.schema import L402CredentialPayload, ManualHoldReason

if TYPE_CHECKING:
    from routeweiler.credentials.schema import CredentialRecord

_log = logging.getLogger(__name__)


class ManifestRecoveryStrategy:
    """Recover a failed retry by consulting service-shape manifests.

    When the original retry returns 4xx, this strategy:
    1. Looks up the service shape matching the credential's challenge URL domain.
    2. For each flow step whose challenge_path matches the URL path, builds an
       alternate fulfilment URL and replays the existing L402 credential against it.
    3. On the first 2xx the credential is considered redeemed.
    4. On exhaustion returns EXHAUSTED.

    The strategy makes its own HTTP calls via ``client`` — a plain httpx.AsyncClient
    with no Routeweiler auth attached, since we are replaying an *existing* credential
    rather than paying a new 402.
    """

    def __init__(
        self,
        registry: ManifestRegistry,
        client: httpx.AsyncClient,
        max_attempts: int = 3,
    ) -> None:
        self._registry = registry
        self._client = client
        self._max_attempts = max_attempts

    async def recover(
        self,
        credential: CredentialRecord,
        last_response: httpx.Response | None,
    ) -> RecoveryOutcome:
        shape = self._registry.lookup(credential.challenge_url)
        if shape is None:
            _log.debug(
                "No service-shape manifest found for %r; recovery exhausted.",
                credential.challenge_url,
            )
            return RecoveryOutcome(
                succeeded=False, response=None, reason=ManualHoldReason.EXHAUSTED
            )

        parsed = urllib.parse.urlparse(credential.challenge_url)
        url_path = parsed.path
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        auth_header = _build_authorization_header(credential)
        if auth_header is None:
            _log.warning(
                "Credential %r is missing macaroon/preimage_hex; cannot build L402 header.",
                credential.credential_id,
            )
            return RecoveryOutcome(
                succeeded=False, response=None, reason=ManualHoldReason.EXHAUSTED
            )

        attempts = 0
        for step in shape.flow:
            if not fnmatch.fnmatchcase(url_path, step.challenge_path):
                continue
            if attempts >= self._max_attempts:
                break

            order_id = step.extract_id(url_path)
            if order_id is None:
                _log.debug(
                    "id_extractor %r did not match path %r; skipping step.",
                    step.id_extractor,
                    url_path,
                )
                continue

            try:
                fulfil_path = step.fulfil_path_template.format_map({"order_id": order_id})
            except KeyError as exc:
                _log.warning(
                    "fulfil_path_template %r references unknown key %s; skipping step.",
                    step.fulfil_path_template,
                    exc,
                )
                continue

            fulfil_url = base_url + fulfil_path
            attempts += 1

            try:
                resp = await self._client.request(
                    method=step.method,
                    url=fulfil_url,
                    headers={"Authorization": auth_header},
                )
            except httpx.RequestError as exc:
                _log.warning(
                    "Recovery request to %r failed with transport error: %s",
                    fulfil_url,
                    exc,
                )
                continue

            if resp.is_success:
                _log.info(
                    "Split-URL recovery succeeded: credential %r redeemed at %r.",
                    credential.credential_id,
                    fulfil_url,
                )
                return RecoveryOutcome(succeeded=True, response=resp, reason=None)

            _log.debug(
                "Recovery attempt %d to %r returned %d; continuing.",
                attempts,
                fulfil_url,
                resp.status_code,
            )

        return RecoveryOutcome(succeeded=False, response=None, reason=ManualHoldReason.EXHAUSTED)


def _build_authorization_header(credential: CredentialRecord) -> str | None:
    """Reconstruct the L402 Authorization header value from a persisted credential payload."""
    typed: L402CredentialPayload = credential.payload  # type: ignore[assignment]
    macaroon = typed.get("macaroon")
    preimage_hex = typed.get("preimage_hex")
    if not macaroon or not preimage_hex:
        return None
    return f"L402 {macaroon}:{preimage_hex}"
