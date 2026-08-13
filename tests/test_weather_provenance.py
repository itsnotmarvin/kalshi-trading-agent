from urllib.parse import parse_qs, urlsplit

import httpx

from core.forecaster import ForecasterInsight
from core.weather_engine import (
    GFS_ENSEMBLE_MODEL,
    HRRR_CONUS_MODEL,
    WeatherEngine,
)


def test_wfo_parser_matches_exact_weather_ticker_city_segment():
    forecaster = ForecasterInsight.__new__(ForecasterInsight)

    assert forecaster._get_wfo_code("KXSNOWSLC-26AUG08") == "SLC"
    assert forecaster._get_wfo_code("KXRAINNO-26AUG08") == "LIX"
    assert forecaster._get_wfo_code("KXHIGHNY-26AUG08-T90") == "OKX"
    assert forecaster._get_wfo_code("KXNFLHOU-26AUG08") is None


def test_nws_discussion_provenance_uses_exact_product_url():
    product_url = "https://api.weather.gov/products/abc-123"

    def handler(request):
        if request.url.path.endswith("/products/types/AFD/locations/SLC"):
            return httpx.Response(200, request=request, json={"@graph": [{"@id": product_url}]})
        return httpx.Response(200, request=request, json={"productText": "Dry air arriving."})

    forecaster = ForecasterInsight.__new__(ForecasterInsight)
    forecaster.api_key = None
    forecaster.client = httpx.Client(transport=httpx.MockTransport(handler))

    insight = forecaster.get_market_insight(
        "KXSNOWSLC-26AUG08", "snowfall", 1.0, True
    )

    assert insight is not None
    assert insight["source"]["office"] == "SLC"
    assert insight["source"]["url"] == product_url
    assert insight["source"]["retrieved_at"]


def test_forecast_requests_explicit_gfs_and_hrrr_models():
    requested_urls = []

    def handler(request):
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            request=request,
            json={"current": {}, "hourly": {"time": [], "temperature_2m": []}},
        )

    engine = WeatherEngine()
    engine.client.close()
    engine.client = httpx.Client(transport=httpx.MockTransport(handler))

    ensemble = engine.get_ensemble_forecast(40.71, -74.01)
    deterministic = engine.get_deterministic_forecast(40.71, -74.01)

    ensemble_query = parse_qs(urlsplit(requested_urls[0]).query)
    deterministic_query = parse_qs(urlsplit(requested_urls[1]).query)
    assert ensemble_query["models"] == [GFS_ENSEMBLE_MODEL]
    assert deterministic_query["models"] == [HRRR_CONUS_MODEL]
    assert ensemble["_provenance"]["model"] == GFS_ENSEMBLE_MODEL
    assert ensemble["_provenance"]["member_count_expected"] == 31
    assert deterministic["_provenance"]["model"] == HRRR_CONUS_MODEL
    assert "models=ncep_hrrr_conus" in deterministic["_provenance"]["url"]
