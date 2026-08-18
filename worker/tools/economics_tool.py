import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from langchain_mistralai import ChatMistralAI

from worker.helpers.http_cache import cached_json
from worker.helpers.json_utils import parse_json
from worker.helpers.messages import business_context, format_transcript
from worker.prompts.economics import (
    ECONOMICS_PLAN_TEMPLATE,
    ECONOMICS_SUMMARY_TEMPLATE,
)
from worker.tools.base import Tool

MAX_OBSERVATIONS = 12  # how many data points are kept in the message/card

BLS_SOURCE = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FRANKFURTER_SOURCE = "https://api.frankfurter.dev/v2/rates"

_OPERATIONS = {
    "world_bank_indicator",
    "world_bank_country_profile",
    "bls_time_series",
    "exchange_rate_series",
}


async def fetch_world_bank_indicator(
    country_code: str, indicator_code: str, start_year: int = 2015, end_year: int | None = None
) -> dict:
    """Fetches a World Bank indicator time series for a country or region."""
    end_year = end_year or datetime.now(UTC).date().year
    if start_year > end_year:
        raise ValueError("start_year cannot be after end_year")
    url = (
        f"https://api.worldbank.org/v2/country/{country_code}/indicator/"
        f"{indicator_code}"
    )
    payload = await cached_json(
        "GET",
        url,
        params={
            "format": "json",
            "date": f"{start_year}:{end_year}",
            "per_page": 1000,
        },
        ttl_seconds=21600,
    )
    if not isinstance(payload, list) or len(payload) < 2:
        return {
            "country_code": country_code,
            "indicator_code": indicator_code,
            "observations": [],
            "source": url,
        }
    metadata, rows = payload[0], payload[1] or []
    observations = [
        {
            "year": int(row["date"]),
            "value": row.get("value"),
            "country": row.get("country", {}).get("value"),
            "unit": row.get("unit"),
            "observation_status": row.get("obs_status"),
        }
        for row in rows
        if row.get("date")
    ]
    observations.sort(key=lambda item: item["year"])
    return {
        "country_code": country_code.upper(),
        "indicator_code": indicator_code,
        "indicator": rows[0].get("indicator", {}).get("value") if rows else None,
        "page_metadata": metadata,
        "observations": observations,
        "source": url,
    }


async def fetch_world_bank_country_profile(country_code: str) -> dict:
    """Fetches World Bank country metadata such as region and income level."""
    url = f"https://api.worldbank.org/v2/country/{country_code}"
    payload = await cached_json(
        "GET", url, params={"format": "json"}, ttl_seconds=86400
    )
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    if not rows:
        return {"country_code": country_code, "country": None, "source": url}
    row = rows[0]
    return {
        "country_code": row.get("iso2Code"),
        "country": row.get("name"),
        "region": row.get("region", {}).get("value"),
        "income_level": row.get("incomeLevel", {}).get("value"),
        "lending_type": row.get("lendingType", {}).get("value"),
        "capital": row.get("capitalCity"),
        "longitude": row.get("longitude"),
        "latitude": row.get("latitude"),
        "source": url,
    }


async def fetch_bls_time_series(
    series_ids: list[str],
    start_year: int | None = None,
    end_year: int | None = None,
) -> dict:
    """Fetches U.S. labor, inflation, wage, or productivity series from BLS."""
    if not series_ids or len(series_ids) > 20:
        raise ValueError("Provide between 1 and 20 BLS series IDs")
    body: dict[str, Any] = {"seriesid": [str(s) for s in series_ids]}
    if start_year is not None:
        body["startyear"] = str(start_year)
    if end_year is not None:
        body["endyear"] = str(end_year)
    payload = await cached_json(
        "POST", BLS_SOURCE, json_body=body, ttl_seconds=21600
    )
    if payload.get("status") != "REQUEST_SUCCEEDED":
        return {
            "status": payload.get("status"),
            "messages": payload.get("message", []),
            "series": [],
            "source": BLS_SOURCE,
        }
    series = []
    for item in payload.get("Results", {}).get("series", []):
        observations = []
        for row in item.get("data", []):
            raw_value = row.get("value")
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = None
            observations.append(
                {
                    "year": int(row["year"]),
                    "period": row.get("period"),
                    "period_name": row.get("periodName"),
                    "value": value,
                    "raw_value": raw_value,
                    "footnotes": [
                        note.get("text")
                        for note in row.get("footnotes", [])
                        if note and note.get("text")
                    ],
                }
            )
        series.append({"series_id": item.get("seriesID"), "observations": observations})
    return {"status": payload.get("status"), "series": series, "source": BLS_SOURCE}


async def fetch_exchange_rate_series(
    base_currency: str,
    quote_currencies: list[str],
    start_date: str | None = None,
    end_date: str | None = None,
    group: str | None = None,
) -> dict:
    """Fetches current or historical central-bank exchange rates."""
    if not quote_currencies:
        raise ValueError("At least one quote currency is required")
    params: dict[str, Any] = {
        "base": base_currency.upper(),
        "quotes": ",".join(code.upper() for code in quote_currencies),
    }
    if start_date:
        params["from"] = start_date
    if end_date:
        params["to"] = end_date
    if group:
        if group not in {"week", "month"}:
            raise ValueError("group must be 'week' or 'month'")
        params["group"] = group
    payload = await cached_json(
        "GET", FRANKFURTER_SOURCE, params=params, ttl_seconds=21600
    )
    return {
        "base_currency": base_currency.upper(),
        "quote_currencies": [code.upper() for code in quote_currencies],
        "rates": payload,
        "source": FRANKFURTER_SOURCE,
    }


class EconomicsTool(Tool):
    name = "economics"
    description = (
        "Fetch live economic and market data: GDP, inflation, population or other World Bank "
        "indicators, country profiles, U.S. labor/BLS statistics, and currency exchange rates."
    )
    example = "What's India's GDP growth over the last 10 years?"
    suggestion = "Wanna check some economic indicators or exchange rates?"
    requires_context = True

    def __init__(self) -> None:
        self.llm = ChatMistralAI(model="mistral-small-2506", temperature=0.1)

    async def run(self, state: dict) -> list[dict]:
        request = str(state.get("user_input") or "").strip()
        context = business_context(state["messages"])
        transcript = format_transcript(state["messages"])

        plan = await self._plan(request, context, transcript)
        operation = str(plan.get("operation") or "")
        args = plan.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        raw = await self._dispatch(operation, args)
        summary = await self._summarize(request, context, transcript, raw)

        return [
            {
                "role": "USER",
                "agent": "TOOL",
                "type": "economics_request",
                "content": request,
            },
            {
                "role": "ASSISTANT",
                "agent": "TOOL",
                "type": "economics",
                "content": summary,
                "data": _trim(raw),
                "source": raw.get("source", ""),
            },
        ]

    async def _plan(self, request: str, context: dict, transcript: str) -> dict:
        """Asks the LLM which of the four data operations to run and with what args."""
        chain = ECONOMICS_PLAN_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "user_input": request,
                "context": json.dumps(context, indent=2),
                "transcript": transcript,
            }
        )
        plan = parse_json(response.content)
        if not isinstance(plan, dict) or "operation" not in plan:
            raise ValueError(f"Unexpected economics plan: {response.content}")
        return plan

    async def _summarize(
        self, request: str, context: dict, transcript: str, raw: dict
    ) -> str:
        """Asks the LLM to turn the raw data into a concise business-oriented summary."""
        chain = ECONOMICS_SUMMARY_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "request": request,
                "context": json.dumps(context, indent=2),
                "transcript": transcript,
                "data": json.dumps(raw, indent=2),
            }
        )
        return str(response.content or "").strip()

    async def _dispatch(self, operation: str, args: dict) -> dict:
        if operation == "world_bank_indicator":
            return await fetch_world_bank_indicator(
                str(args.get("country_code") or ""),
                str(args.get("indicator_code") or ""),
                _opt_int(args.get("start_year"), 2015),
                _opt_int(args.get("end_year"), None),
            )
        if operation == "world_bank_country_profile":
            return await fetch_world_bank_country_profile(
                str(args.get("country_code") or "")
            )
        if operation == "bls_time_series":
            return await fetch_bls_time_series(
                _list_arg(args.get("series_ids")),
                _opt_int(args.get("start_year"), None),
                _opt_int(args.get("end_year"), None),
            )
        if operation == "exchange_rate_series":
            return await fetch_exchange_rate_series(
                str(args.get("base_currency") or ""),
                _list_arg(args.get("quote_currencies")),
                _opt_str(args.get("start_date")),
                _opt_str(args.get("end_date")),
                _opt_str(args.get("group")),
            )
        raise ValueError(f"Unknown economics operation: {operation!r}")


def _opt_int(value: Any, default: int | None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _list_arg(value: Any) -> list[str]:
    if not isinstance(value, list):
        return [str(value)] if value else []
    return [str(item) for item in value]


def _trim(raw: dict) -> dict:
    """Trims time-series observations so the persisted message and the frontend
    card stay small even when the API returns thousands of data points."""
    result = dict(raw)
    if isinstance(result.get("observations"), list):
        result["observations"] = result["observations"][-MAX_OBSERVATIONS:]
    series = result.get("series")
    if isinstance(series, list):
        result["series"] = [
            (
                {
                    **entry,
                    "observations": (entry.get("observations") or [])[-MAX_OBSERVATIONS:],
                }
                if isinstance(entry, dict)
                else entry
            )
            for entry in series
        ]
    return result
