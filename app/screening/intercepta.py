"""
intercepta.py - REST client for the Intercepta (Web3 Antivirus) risk API.

Endpoints used (https://docs.web3antivirus.io/reference, header X-API-KEY):

    GET  /api/public/v2/extension/account/{address}/quick-scan      Quick Scan Address
    GET  /api/public/v2/extension/account/{address}/toxic-score     Deep Scan Address
         -> {"toxicScore": number, "traits": [{"name", "risk", "txsCount", "description"}]}
    GET  /api/public/v2/extension/token-intelligence/token/{address}/risks?chainId=1
         -> {"action": "block"|"warn"|"info", "riskLevel", "trust", "detectors": [...], ...}
    POST /api/public/v2/extension/analysis/signature                Scan Message
         body {"from": owner, "message": "<EIP-712 JSON string>", "chainId": "1"}
         -> {"riskGroup": "Low"|"Medium"|"High", "detectors": [...], "addresses": [...], ...}

Risk data covers mainnet chains only, so callers pass mainnet addresses (see
app/screening/service.py for the Sepolia -> mainnet mapping).

Every failure raises InterceptaError with a `code`:
    MISSING_KEY   INTERCEPTA_API_KEY is not set (no request is sent)
    TIMEOUT       no answer within SCREENING_TIMEOUT_SECONDS
    UNREACHABLE   connection / transport error
    HTTP_ERROR    non-2xx status (`status` holds it)
    BAD_RESPONSE  body is not JSON or does not match the documented schema

Environment:
    INTERCEPTA_API_KEY          API key (X-API-KEY header)
    INTERCEPTA_BASE_URL         default https://api.web3antivirus.io
    SCREENING_TIMEOUT_SECONDS   per-request timeout, default 4

`http` is anything with requests-style get(url, headers=, params=, timeout=)
and post(url, json=, headers=, timeout=); tests pass a fake. No Flask imports.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.parse import quote

import requests

DEFAULT_BASE_URL = "https://api.web3antivirus.io"
DEFAULT_TIMEOUT_SECONDS = 4.0
MAINNET_CHAIN_ID = "1"

QUICK_SCAN_PATH = "/api/public/v2/extension/account/{address}/quick-scan"
DEEP_SCAN_PATH = "/api/public/v2/extension/account/{address}/toxic-score"
TOKEN_RISKS_PATH = "/api/public/v2/extension/token-intelligence/token/{address}/risks"
SCAN_MESSAGE_PATH = "/api/public/v2/extension/analysis/signature"

ERROR_CODES = frozenset({"MISSING_KEY", "TIMEOUT", "UNREACHABLE", "HTTP_ERROR", "BAD_RESPONSE"})


class InterceptaError(Exception):
    def __init__(self, code: str, message: str, *, status: Optional[int] = None, body: Any = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown InterceptaError code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.body = body


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bad(message: str, body: Any) -> InterceptaError:
    return InterceptaError("BAD_RESPONSE", message, body=body)


def validate_address_scan(body: Any) -> dict:
    """ToxicScoreShortResponseV2: toxicScore (number) and traits[] with a name."""
    if not isinstance(body, dict) or not _is_number(body.get("toxicScore")):
        raise _bad("address scan without a numeric toxicScore", body)
    traits = body.get("traits")
    if not isinstance(traits, list) or not all(
            isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"] for t in traits):
        raise _bad("address scan traits must be a list of objects with a name", body)
    return body


def validate_token_scan(body: Any) -> dict:
    """TokenRiskAnalysisV2Response: the recommended `action` is what we act on."""
    if not isinstance(body, dict) or not isinstance(body.get("action"), str):
        raise _bad("token scan without an action", body)
    if not isinstance(body.get("detectors", []), list):
        raise _bad("token scan detectors must be a list", body)
    return body


def validate_signature_scan(body: Any) -> dict:
    """SignatureAnalysisResponseDTO: riskGroup, detectors[] and addresses[] are required."""
    if not isinstance(body, dict) or not isinstance(body.get("riskGroup"), str):
        raise _bad("signature scan without a riskGroup", body)
    for key in ("detectors", "addresses"):
        if not isinstance(body.get(key), list):
            raise _bad(f"signature scan {key} must be a list", body)
    return body


class InterceptaClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: Optional[float] = None, http=requests):
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("INTERCEPTA_API_KEY", "")).strip()
        self.base_url = (base_url or os.environ.get("INTERCEPTA_BASE_URL", "")
                         or DEFAULT_BASE_URL).strip().rstrip("/")
        if timeout is None:
            raw = os.environ.get("SCREENING_TIMEOUT_SECONDS", "").strip()
            timeout = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
        self.timeout = timeout
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    # ── endpoints ────────────────────────────────────────────────────────────
    def quick_scan_address(self, address: str) -> dict:
        return validate_address_scan(self._get(QUICK_SCAN_PATH.format(address=quote(address))))

    def deep_scan_address(self, address: str) -> dict:
        return validate_address_scan(self._get(DEEP_SCAN_PATH.format(address=quote(address))))

    def scan_token(self, address: str, chain_id: str = MAINNET_CHAIN_ID) -> dict:
        path = TOKEN_RISKS_PATH.format(address=quote(address))
        return validate_token_scan(self._get(path, params={"chainId": str(chain_id)}))

    def scan_message(self, *, owner: str, typed_data: dict,
                     chain_id: str = MAINNET_CHAIN_ID, website: Optional[str] = None) -> dict:
        """Scan an EIP-712 payload; the API takes it as a JSON *string*."""
        body = {"from": owner, "message": json.dumps(typed_data, separators=(",", ":")),
                "chainId": str(chain_id)}
        if website:
            body["website"] = website
        return validate_signature_scan(self._request("POST", SCAN_MESSAGE_PATH, json=body))

    # ── transport ────────────────────────────────────────────────────────────
    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.configured:
            raise InterceptaError("MISSING_KEY", "INTERCEPTA_API_KEY is not set")
        url = self.base_url + path
        headers = {"X-API-KEY": self.api_key, "Accept": "application/json"}
        try:
            if method == "GET":
                resp = self.http.get(url, headers=headers, timeout=self.timeout, **kwargs)
            else:
                resp = self.http.post(url, headers=headers, timeout=self.timeout, **kwargs)
        except requests.Timeout as exc:
            raise InterceptaError("TIMEOUT", f"no answer within {self.timeout:g}s") from exc
        except requests.RequestException as exc:
            raise InterceptaError("UNREACHABLE", type(exc).__name__) from exc
        if not 200 <= resp.status_code < 300:
            raise InterceptaError("HTTP_ERROR", f"HTTP {resp.status_code}",
                                  status=resp.status_code, body=_safe_body(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise InterceptaError("BAD_RESPONSE", "response is not JSON",
                                  body=_safe_body(resp)) from exc


def _safe_body(resp) -> Any:
    """Parsed JSON if possible, else the first 500 characters of text."""
    try:
        return resp.json()
    except ValueError:
        return (getattr(resp, "text", "") or "")[:500]
