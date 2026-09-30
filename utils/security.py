import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from schemas.messages import SignedOrderPayload

_MINIMUM_SECRET_BYTES = 32
_SECRET_ENVIRONMENT_VARIABLE = "ORDER_SIGNING_SECRET"


def _secret_bytes(secret: str | bytes | None) -> bytes:
    if secret is None:
        secret = os.environ.get(_SECRET_ENVIRONMENT_VARIABLE)
    if secret is None:
        raise ValueError(
            f"Provide a signing secret or set {_SECRET_ENVIRONMENT_VARIABLE}."
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


def _canonical_payload(payload: SignedOrderPayload | Mapping[str, Any]) -> bytes:
    if isinstance(payload, SignedOrderPayload):
        values = payload.model_dump(mode="json", exclude={"signature"})
    elif isinstance(payload, Mapping):
        values = dict(payload)
        values.pop("signature", None)
    else:
        raise TypeError("payload must be SignedOrderPayload or a mapping")

    try:
        return json.dumps(
            values,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain only finite JSON values") from exc


def sign_payload(
    payload: SignedOrderPayload,
    secret: str | bytes | None = None,
) -> SignedOrderPayload:
    key = _secret_bytes(secret)
    digest = hmac.new(key, _canonical_payload(payload), hashlib.sha256).hexdigest()
    return payload.model_copy(update={"signature": digest})


def verify_payload(
    payload: SignedOrderPayload,
    secret: str | bytes | None = None,
) -> bool:
    if payload.signature is None:
        return False
    key = _secret_bytes(secret)
    expected = hmac.new(key, _canonical_payload(payload), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, payload.signature)
