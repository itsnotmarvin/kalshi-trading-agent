import httpx
import os
import json
import re
from datetime import datetime, timezone
import anthropic
from config.settings import settings

# Mapping of Kalshi city codes to NWS Weather Forecast Office (WFO) codes
# NWS API requires identifying the office to get the discussion
CITY_TO_WFO = {
    "CHI": "LOT", # Chicago (Romeoville)
    "NY": "OKX",  # New York City (Upton)
    "LA": "LOX",  # Los Angeles / Oxnard
    "MIA": "MFL", # Miami / South Florida
    "HOU": "HGX", # Houston / Galveston
    "DEN": "BOU", # Denver / Boulder
    "BOS": "BOX", # Boston / Norton
    "ATL": "FFC", # Atlanta / Peachtree City
    "SF": "MTR",  # San Francisco Monterey
    "DC": "LWX",  # Baltimore / Washington
    "PHI": "PHI", # Philadelphia / Mt Holly
    "DAL": "FWD", # Dallas / Fort Worth
    "PHX": "PSR", # Phoenix
    "SEA": "SEW", # Seattle
    "DET": "DTX", # Detroit / Pontiac
    "MIN": "MPX", # Minneapolis
    "TPA": "TBW", # Tampa Bay Area
    "POR": "PQR", # Portland
    "AUS": "EWX", # Austin / San Antonio
    "LV": "VEF",  # Las Vegas
    "NO": "LIX",  # New Orleans
    "SLC": "SLC", # Salt Lake City
    "SAC": "STO", # Sacramento
    "OKC": "OUN"  # Oklahoma City
}

WEATHER_TICKER_CITY_RE = re.compile(
    r"^KX(?:HIGH|LOW|RAIN|SNOW|WIND)([A-Z]+?)(?:-|$)"
)

class ForecasterInsight:
    """
    Fetches the latest Area Forecast Discussion from the National Weather Service
    and uses Claude to extract qualitative context on specific variables.
    """
    
    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            print("Warning: ANTHROPIC_API_KEY not found. Forecaster insight will be disabled.")
        
        self.client = httpx.Client(
            headers={"User-Agent": "KalshiWeatherBot/1.0 (kalshi-agent)"},
            timeout=30.0,
            transport=httpx.HTTPTransport(retries=3)
        )
        
    def _get_wfo_code(self, market_id: str) -> str | None:
        """Extract WFO code from a Kalshi weather market ID."""
        match = WEATHER_TICKER_CITY_RE.match(str(market_id).upper())
        if not match:
            return None
        return CITY_TO_WFO.get(match.group(1))

    def fetch_nws_discussion(self, wfo_code: str) -> str | None:
        """Fetch the latest Area Forecast Discussion for a given NWS office."""
        record = self._fetch_nws_discussion_record(wfo_code)
        return record["text"] if record else None

    def _fetch_nws_discussion_record(self, wfo_code: str) -> dict | None:
        """Fetch discussion text together with the exact NWS product URL."""
        try:
            url = f"https://api.weather.gov/products/types/AFD/locations/{wfo_code}"
            resp = self.client.get(url)
            resp.raise_for_status()
            products = resp.json().get("@graph", [])
            
            if not products:
                return None
                
            # Get the latest discussion URL
            latest_url = products[0].get("@id")
            if not latest_url:
                return None
                
            text_resp = self.client.get(latest_url)
            text_resp.raise_for_status()
            
            text = text_resp.json().get("productText")
            if not text:
                return None
            return {
                "text": text,
                "url": latest_url,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            print(f"Error fetching NWS discussion for {wfo_code}: {e}")
            return None

    def analyze_sentiment(self, text: str, variable: str, threshold: float, above: bool) -> dict:
        """
        Use Claude to read the NWS discussion and determine if human forecasters
        are leaning hotter/colder or wetter/drier than models.
        """
        if not self.api_key or not text:
            return {"modifier": 0.0, "reasoning": "No API key or text available."}
            
        direction_str = "above" if above else "below"
        var_str = "temperature" if variable == "temperature_2m" else variable
        
        prompt = f"""
You are an expert meteorologist analyzing a National Weather Service Area Forecast Discussion.
I need to price a prediction market on whether the {var_str} will be {direction_str} {threshold}.

Here is the latest NWS Area Forecast Discussion:
```
{text}
```

Based strictly on this discussion, are the forecasters indicating that actual conditions might deviate from standard automated models regarding this threshold? For example, are they noting a "lake breeze" that drops temperatures, or "cloud cover breaking early" that spikes temperatures?

Provide your analysis in the following JSON format:
{{
    "reasoning": "<1 sentence explanation of the relevant meteorological phenomenon>"
}}

- State only relevant qualitative evidence. Do not assign a numeric probability adjustment.
- Keep the reasoning extremely concise (max 1 sentence).
"""

        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=settings.claude_model,
                max_tokens=200,
                thinking={"type": "disabled"},
                system="You are a data extraction AI. Output ONLY valid JSON, nothing else.",
                messages=[{"role": "user", "content": prompt}]
            )
            
            content = response.content[0].text.strip()
            # Clean up potential markdown formatting
            if content.startswith("```json"):
                content = content[7:-3]
            elif content.startswith("```"):
                content = content[3:-3]
                
            result = json.loads(content)
            
            return {
                "modifier": 0.0,
                "reasoning": result.get("reasoning", "Parsed discussion.")
            }
            
        except Exception as e:
            print(f"Error analyzing sentiment via Claude: {e}")
            return {"modifier": 0.0, "reasoning": f"Analysis failed: {e}"}

    def get_market_insight(self, market_id: str, variable: str, threshold: float, above: bool) -> dict | None:
        """Main entry point to get human insight for a specific market."""
        wfo = self._get_wfo_code(market_id)
        if not wfo:
            return None
            
        discussion = self._fetch_nws_discussion_record(wfo)
        if not discussion:
            return None
            
        insight = self.analyze_sentiment(discussion["text"], variable, threshold, above)
        insight["source"] = {
            "source_type": "forecast_discussion",
            "provider": "National Weather Service",
            "office": wfo,
            "url": discussion["url"],
            "retrieved_at": discussion["retrieved_at"],
        }
        return insight
