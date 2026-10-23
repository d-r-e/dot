"""Keyless weather lookups through Open-Meteo's two fixed HTTPS endpoints.

No URL comes from the model or the user: the only variable parts are query
parameters. That keeps this outside the browser's SSRF surface while giving the
assistant a reliable answer for a common spoken request (the browser cannot
search, and search engines block headless clients).
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = float(os.environ.get("DOT_WEATHER_TIMEOUT", "8"))
MAX_PLACE_CHARS = 100

_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre")
_WMO = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado",
    3: "nublado", 45: "niebla", 48: "niebla con escarcha",
    51: "llovizna ligera", 53: "llovizna", 55: "llovizna intensa",
    56: "llovizna helada", 57: "llovizna helada intensa",
    61: "lluvia ligera", 63: "lluvia", 65: "lluvia intensa",
    66: "lluvia helada", 67: "lluvia helada intensa",
    71: "nieve ligera", 73: "nieve", 75: "nieve intensa", 77: "granos de nieve",
    80: "chubascos ligeros", 81: "chubascos", 82: "chubascos intensos",
    85: "chubascos de nieve", 86: "chubascos de nieve intensos",
    95: "tormenta", 96: "tormenta con granizo", 99: "tormenta fuerte con granizo",
}


class WeatherError(Exception):
    """A model-visible weather failure; never permission to guess a forecast."""


async def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(
        timeout=TIMEOUT, trust_env=False, follow_redirects=False,
        headers={"User-Agent": "dot-weather/1"},
    ) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    return data


def _condition(code: Any) -> str:
    try:
        return _WMO.get(int(code), "condiciones variables")
    except (TypeError, ValueError):
        return "condiciones variables"


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value)


def _label(day: dt.date, index: int) -> str:
    prefix = ("Hoy", "Mañana", "Pasado mañana")[index]
    return f"{prefix}, {_WEEKDAYS[day.weekday()]} {day.day} de {_MONTHS[day.month - 1]}"


def _place_name(location: dict[str, Any]) -> str:
    parts = [str(location["name"])] if location.get("name") else []
    for key in ("admin1", "country"):
        value = location.get(key)
        if value and str(value) not in parts:
            parts.append(str(value))
    return ", ".join(parts) or "el lugar indicado"


def _summary(location: dict[str, Any], data: dict[str, Any]) -> str:
    lines = [f"Tiempo en {_place_name(location)} (datos de Open-Meteo)."]
    current = data.get("current") or {}
    if isinstance(current, dict) and current:
        bits = []
        if (temperature := _number(current.get("temperature_2m"))) is not None:
            bits.append(f"{temperature} °C")
        if current.get("weather_code") is not None:
            bits.append(_condition(current["weather_code"]))
        if (wind := _number(current.get("wind_speed_10m"))) is not None:
            bits.append(f"viento {wind} km/h")
        if (humidity := _number(current.get("relative_humidity_2m"))) is not None:
            bits.append(f"humedad {humidity}%")
        if bits:
            lines.append("Ahora: " + ", ".join(bits) + ".")

    daily = data.get("daily") or {}
    times = daily.get("time") if isinstance(daily, dict) else None
    for index, value in enumerate(times[:3] if isinstance(times, list) else []):
        try:
            day = dt.date.fromisoformat(str(value))
        except ValueError:
            continue
        bits = []
        if index < len(daily.get("weather_code") or []):
            bits.append(_condition(daily["weather_code"][index]))
        low = _number((daily.get("temperature_2m_min") or [None] * 3)[index])
        high = _number((daily.get("temperature_2m_max") or [None] * 3)[index])
        if low is not None and high is not None:
            bits.append(f"mínima {low} °C, máxima {high} °C")
        if index < len(daily.get("precipitation_probability_max") or []):
            rain = _number(daily["precipitation_probability_max"][index])
            if rain is not None:
                bits.append(f"{rain}% de lluvia")
        if index < len(daily.get("wind_speed_10m_max") or []):
            wind = _number(daily["wind_speed_10m_max"][index])
            if wind is not None:
                bits.append(f"viento hasta {wind} km/h")
        if bits:
            lines.append(f"{_label(day, index)}: " + ", ".join(bits) + ".")
    return "\n".join(lines)


async def weather(place: str) -> str:
    place = place.strip() if isinstance(place, str) else ""
    if not place or len(place) > MAX_PLACE_CHARS or any(ord(char) < 32 for char in place):
        raise WeatherError("indica un lugar válido de hasta 100 caracteres")

    try:
        geo = await _get_json(GEOCODE_URL, {
            "name": place, "count": 1, "language": "es", "format": "json",
        })
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherError("no se pudo consultar el servicio meteorológico; inténtalo de nuevo") from exc

    results = geo.get("results")
    if not isinstance(results, list) or not results:
        raise WeatherError(f"no encuentro el lugar «{place}»; prueba con la ciudad y el país")
    location = results[0]
    latitude, longitude = location.get("latitude"), location.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise WeatherError("el servicio meteorológico no devolvió coordenadas válidas")

    try:
        data = await _get_json(FORECAST_URL, {
            "latitude": latitude, "longitude": longitude,
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "precipitation_probability_max,wind_speed_10m_max"),
            "timezone": "auto", "forecast_days": 3, "wind_speed_unit": "kmh",
        })
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherError("no se pudo obtener la previsión; inténtalo de nuevo") from exc

    return _summary(location, data)
