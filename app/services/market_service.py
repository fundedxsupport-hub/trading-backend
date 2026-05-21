from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException, status

from app.config import get_settings

settings = get_settings()


def _headers() -> Dict[str, str]:
    if not settings.upstox_access_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upstox access token is not configured")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.upstox_access_token}",
    }


def resolve_underlying_key(underlying: str) -> str:
    normalized = underlying.strip().upper().replace(" ", "")
    if "|" in underlying:
        return underlying.strip()
    underlying_key = settings.parsed_underlying_map.get(normalized)
    if not underlying_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported underlying. Use NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, MIDCAP, SENSEX, or BANKEX.",
        )
    return underlying_key


def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{settings.upstox_base_url.rstrip('/')}{path}"
    try:
        response = requests.get(url, headers=_headers(), params=params, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    except requests.RequestException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def list_option_contracts(underlying: str, expiry_date: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"instrument_key": resolve_underlying_key(underlying)}
    if expiry_date:
        params["expiry_date"] = expiry_date
    payload = _get("/option/contract", params)
    contracts = payload.get("data") or []
    expiries = sorted({item.get("expiry") for item in contracts if item.get("expiry")})
    return {
        "underlying": underlying.strip().upper(),
        "underlying_key": params["instrument_key"],
        "expiries": expiries,
        "contracts": contracts,
    }


def _normalize_option(option: Optional[Dict[str, Any]], option_type: str) -> Optional[Dict[str, Any]]:
    if not option:
        return None
    market_data = option.get("market_data") or {}
    return {
        "option_type": option_type,
        "instrument_key": option.get("instrument_key"),
        "ltp": market_data.get("ltp"),
        "bid_price": market_data.get("bid_price"),
        "ask_price": market_data.get("ask_price"),
        "volume": market_data.get("volume"),
        "oi": market_data.get("oi"),
        "market_data": market_data,
        "option_greeks": option.get("option_greeks") or {},
    }


def get_option_chain(underlying: str, expiry_date: str) -> Dict[str, Any]:
    underlying_key = resolve_underlying_key(underlying)
    payload = _get("/option/chain", {"instrument_key": underlying_key, "expiry_date": expiry_date})
    rows: List[Dict[str, Any]] = []
    for item in payload.get("data") or []:
        rows.append(
            {
                "expiry": item.get("expiry"),
                "strike_price": item.get("strike_price"),
                "underlying_key": item.get("underlying_key"),
                "underlying_spot_price": item.get("underlying_spot_price"),
                "pcr": item.get("pcr"),
                "call": _normalize_option(item.get("call_options"), "CE"),
                "put": _normalize_option(item.get("put_options"), "PE"),
            }
        )
    return {
        "underlying": underlying.strip().upper(),
        "underlying_key": underlying_key,
        "expiry_date": expiry_date,
        "rows": rows,
    }
