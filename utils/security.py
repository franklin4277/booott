import hashlib
import hmac
import os
from collections.abc import Mapping, Sequence
from typing import Any

from schemas.messages import SignedOrderPayload

_MINIMUM_SECRET_BYTES = 32
_SECRET_ENVIRONMENT_VARIABLES = ("ORDER_SIGNING_SECRET", "SECRET_KEY")
HMAC_FIELD_ORDER: tuple[str, ...] = (
    "order_id",
    "intent_id",
    "symbol",
    "side",
    "order_type",
    "volume",
    "price",
    "stop_loss",
    "take_profit",
    "issued_at",
    "expires_at",
    "nonce",
    "trace_id",
)


def _secret_bytes(secret: str | bytes | None) -> bytes:
    if secret is None:
        for name in _SECRET_ENVIRONMENT_VARIABLES:
            secret = os.environ.get(name)
            if secret:
                break
    if secret is None:
        raise ValueError(
            "Provide a signing secret or set ORDER_SIGNING_SECRET / SECRET_KEY."
        )

    if isinstance(secret, str):
        encoded = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        encoded = secret
    else:
        raise TypeError("signing secret must be a string or bytes")
    if len(encoded) < _MINIMUM_SECRET_BYTES:
        raise ValueError("HMAC signing secret must be at least 32 bytes.")
    return encoded


def _json_values(payload: SignedOrderPayload | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, SignedOrderPayload):
        return payload.model_dump(mode="json", exclude={"signature"})
    if isinstance(payload, Mapping):
        values = dict(payload)
        values.pop("signature", None)
        return values
    raise TypeError("payload must be SignedOrderPayload or a mapping")


def canonical_hmac_message(payload: SignedOrderPayload | Mapping[str, Any]) -> str:
    """Stable pipe-delimited message used by Python services and MQL5 EAs."""
    values = _json_values(payload)
    parts: list[str] = []
    for key in HMAC_FIELD_ORDER:
        value = values.get(key)
        parts.append("" if value is None else str(value))
    return "|".join(parts)


def _canonical_payload(payload: SignedOrderPayload | Mapping[str, Any]) -> bytes:
    try:
        return canonical_hmac_message(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain only finite JSON values") from exc


def generate_hmac_sha256(
    payload: SignedOrderPayload | Mapping[str, Any],
    secret: str | bytes | None = None,
) -> str:
    key = _secret_bytes(secret)
    return hmac.new(key, _canonical_payload(payload), hashlib.sha256).hexdigest()


def verify_hmac_sha256(
    payload: SignedOrderPayload | Mapping[str, Any],
    signature: str | None,
    secret: str | bytes | None = None,
) -> bool:
    if signature is None or len(signature) != 64:
        return False
    expected = generate_hmac_sha256(payload, secret)
    return hmac.compare_digest(expected, signature)


def sign_payload(
    payload: SignedOrderPayload,
    secret: str | bytes | None = None,
) -> SignedOrderPayload:
    digest = generate_hmac_sha256(payload, secret)
    return payload.model_copy(update={"signature": digest})


def verify_payload(
    payload: SignedOrderPayload,
    secret: str | bytes | None = None,
) -> bool:
    return verify_hmac_sha256(payload, payload.signature, secret)


def signed_fields(payload: SignedOrderPayload) -> Sequence[str]:
    return HMAC_FIELD_ORDER
