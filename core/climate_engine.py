import httpx
from datetime import datetime, timezone

class ClimateEngine:
    """
    Fetches authoritative, high-level climate/weather products from the National Weather Service (NWS)
    and National Hurricane Center (NHC) to inject quantitative Ground Truth into Claude's prompt
    for General Climate markets on Kalshi.
    """
    def __init__(self):
        self.client = httpx.Client(
            headers={"User-Agent": "KalshiPredictionBot/1.0 (kalshi-agent)"},
            timeout=30.0,
            transport=httpx.HTTPTransport(retries=2)
        )
        
        # Keys are NWS product types, Values are descriptions
        self.products = {
            "TWO": "Tropical Weather Outlook (Hurricane Tracking)",
            "PMD": "Extended Range Prognostic Discussion (8-14 Day Temp/Precip)",
            "PMDMRD": "Monthly and Seasonal Prognostic Discussion"
        }

    def fetch_latest_product(self, product_type: str) -> str | None:
        """Fetch the raw text of the latest NWS product of a given type."""
        try:
            url = f"https://api.weather.gov/products/types/{product_type}"
            resp = self.client.get(url)
            resp.raise_for_status()
            
            data = resp.json().get("@graph", [])
            if not data:
                return None
                
            latest_url = data[0].get("@id")
            if not latest_url:
                return None
                
            text_resp = self.client.get(latest_url)
            text_resp.raise_for_status()
            
            return text_resp.json().get("productText")
        except Exception as e:
            print(f"⚠️  ClimateEngine failed to fetch {product_type}: {e}")
            return None

    def get_climate_context(self) -> str:
        """
        Gathers context from all relevant climate products and bundles them into a concise string.
        """
        context_parts = []
        
        for p_type, description in self.products.items():
            raw_text = self.fetch_latest_product(p_type)
            if raw_text:
                # Extract first ~1000 characters to capture the main summary without blowing up token counts
                content = raw_text[:1000].strip()
                if len(raw_text) > 1000:
                    content += "...\n[TRUNCATED: Use web search for full details]"
                    
                context_parts.append(f"--- {description} ---\n{content}\n")
            
        if not context_parts:
            return "No official NOAA/NHC climate products were currently readable. You MUST rely heavily on your web search tool to find current Tropical Weather Outlooks and NOAA CPC predictions."
            
        return "\n".join(context_parts)
