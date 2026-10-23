import unittest
from unittest.mock import AsyncMock, patch

import httpx

import tools
import weather_tool
from tools import BLOCKING_TOOLS, Toolbox, tool_specs
from weather_tool import WeatherError, weather

GEO = {"results": [{
    "name": "Madrid", "admin1": "Comunidad de Madrid", "country": "España",
    "latitude": 40.4168, "longitude": -3.7038,
}]}
FORECAST = {
    "current": {
        "temperature_2m": 15.4, "weather_code": 0,
        "wind_speed_10m": 6.2, "relative_humidity_2m": 60,
    },
    "daily": {
        "time": ["2026-10-23", "2026-10-24", "2026-10-25"],
        "weather_code": [0, 61, 3],
        "temperature_2m_max": [24.2, 21.0, 19.6],
        "temperature_2m_min": [12.1, 13.4, 11.0],
        "precipitation_probability_max": [0, 70, 10],
        "wind_speed_10m_max": [14.3, 20.0, 9.2],
    },
}


class WeatherSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_spanish_summary_of_three_days(self):
        with patch.object(weather_tool, "_get_json", AsyncMock(side_effect=[GEO, FORECAST])) as get:
            text = await weather("Madrid")
        self.assertEqual(get.await_count, 2)
        self.assertIn("Tiempo en Madrid, Comunidad de Madrid, España", text)
        self.assertIn("Ahora: 15 °C, despejado, viento 6 km/h, humedad 60%.", text)
        self.assertIn("Hoy, viernes 23 de octubre: despejado, mínima 12 °C, máxima 24 °C", text)
        self.assertIn("Mañana, sábado 24 de octubre: lluvia ligera, mínima 13 °C, máxima 21 °C, "
                      "70% de lluvia, viento hasta 20 km/h.", text)
        self.assertIn("Pasado mañana, domingo 25 de octubre: nublado", text)
        forecast_params = get.await_args_list[1].args[1]
        self.assertEqual(forecast_params["forecast_days"], 3)
        self.assertEqual(forecast_params["timezone"], "auto")
        self.assertNotIn("api_key", forecast_params)

    async def test_unknown_place_and_upstream_failure(self):
        with patch.object(weather_tool, "_get_json", AsyncMock(return_value={"results": []})):
            with self.assertRaisesRegex(WeatherError, "no encuentro"):
                await weather("Atlántida")
        with patch.object(weather_tool, "_get_json", AsyncMock(side_effect=httpx.ConnectError("down"))):
            with self.assertRaisesRegex(WeatherError, "no se pudo consultar"):
                await weather("Madrid")

    async def test_invalid_place_never_reaches_network(self):
        with patch.object(weather_tool, "_get_json", AsyncMock()) as get:
            for place in ["", "   ", "x" * 101, "Madrid\nMadrid"]:
                with self.subTest(place=place), self.assertRaises(WeatherError):
                    await weather(place)
            get.assert_not_awaited()

    async def test_partial_forecast_does_not_crash(self):
        partial = {"daily": {"time": ["2026-10-23"], "weather_code": [2]}}
        with patch.object(weather_tool, "_get_json", AsyncMock(side_effect=[GEO, partial])):
            text = await weather("Madrid")
        self.assertIn("Hoy, viernes 23 de octubre: parcialmente nublado.", text)


class WeatherToolWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_spec_available_without_browser(self):
        with patch.dict("os.environ", {"DOT_TOOLS": "1", "DOT_BROWSE": "0"}):
            names = {spec["function"]["name"] for spec in tool_specs()}
        self.assertIn("weather", names)
        self.assertNotIn("navigate", names)
        self.assertIn("weather", BLOCKING_TOOLS)

    async def test_dispatch_passes_place(self):
        toolbox = Toolbox()
        try:
            with patch.object(tools, "lookup_weather", AsyncMock(return_value="previsión")) as lookup:
                result = await toolbox.call("weather", '{"place":"Valencia, España"}')
            self.assertEqual(result, "previsión")
            lookup.assert_awaited_once_with("Valencia, España")
        finally:
            await toolbox.close()

    async def test_dispatch_surfaces_weather_error(self):
        toolbox = Toolbox()
        try:
            with patch.object(tools, "lookup_weather", AsyncMock(side_effect=WeatherError("sin datos"))):
                result = await toolbox.call("weather", '{"place":"Madrid"}')
            self.assertEqual(result, "error: sin datos")
        finally:
            await toolbox.close()


if __name__ == "__main__":
    unittest.main()
