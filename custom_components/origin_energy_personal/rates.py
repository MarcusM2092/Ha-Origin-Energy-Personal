"""Normalise Origin tariff rates into stable, entity-friendly records.

Pure Python (no Home Assistant imports) so it can be unit tested.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .const import RATES_IN_CENTS

_TOD_LABELS = {
    "PEAK": "peak rate",
    "SHOULDER": "shoulder rate",
    "OFFPEAK": "off-peak rate",
    "ALLDAY": "anytime rate",
}


def _slug(*parts: Any) -> str:
    text = "_".join(str(p) for p in parts if p not in (None, ""))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _to_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def normalize_rates(fuel: str, rates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Turn raw API rates into records with a stable key, name and value.

    Uses the GST-inclusive rate (what bills and the website show), falling
    back to the exclusive one. Rates with no usable number are dropped.
    """
    out: list[dict[str, Any]] = []
    for raw in rates or []:
        if not isinstance(raw, dict):
            continue
        inc = _to_float(raw.get("rateIncludingTax"))
        exc = _to_float(raw.get("rateExcludingTax"))
        value = inc if inc is not None else exc
        if value is None:
            continue

        band = (raw.get("bandCategory") or "").upper()
        tod = (raw.get("timeOfDay") or "").upper()
        if band == "STANDING_CHARGE":
            label = "supply charge"
        else:
            label = _TOD_LABELS.get(tod, f"{tod.lower().replace('_', ' ') or 'usage'} rate")

        step_s, step_e = raw.get("stepStart"), raw.get("stepEnd")
        step = ""
        if step_s is not None or step_e is not None:
            step = f" block {step_s if step_s is not None else 0}-{step_e if step_e is not None else 'up'}"

        out.append(
            {
                "fuel": fuel,
                "band": band,
                "time_of_day": tod,
                "value": value,
                "value_ex_tax": exc,
                "code": raw.get("networkTariffCode"),
                "active_from": raw.get("activeFrom"),
                "active_to": raw.get("activeTo"),
                "base_name": f"{fuel.title()} {label}{step}",
                "_key_parts": (fuel, band, tod, step_s, step_e, raw.get("networkTariffCode")),
            }
        )

    # Two rates can share a label (e.g. different network tariff codes):
    # tell them apart by code rather than silently dropping one.
    counts = Counter(r["base_name"] for r in out)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for r in out:
        name = r["base_name"]
        if counts[name] > 1 and r["code"]:
            name = f"{name} ({r['code']})"
        key = _slug(*r.pop("_key_parts"))
        if key in seen:
            continue
        seen.add(key)
        r["name"], r["key"] = name, key
        result.append(r)
    return result


def rate_unit(rate: dict[str, Any], gas_unit: str | None) -> str | None:
    """Display unit for a rate sensor (an assumption: see RATES_IN_CENTS)."""
    money = "c" if RATES_IN_CENTS else "$"
    if rate["band"] == "STANDING_CHARGE":
        return f"{money}/day"
    if rate["fuel"] == "electricity":
        return f"{money}/kWh"
    return f"{money}/{gas_unit}" if gas_unit else None
