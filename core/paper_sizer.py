"""
Exploratory sizing engine for paper/shadow mode.

This module generates randomized trade sizes for the paper bot using a
log-uniform distribution. This allows the bot to place exploratory trades across
various orders of magnitude (e.g., $10, $100, $1,000, $10,000) with equal probability.
The goal is to gather data at different balance and trade sizes to discover the
mathematical break-point for liquidity, fees, and overall trade execution quality
without risking real capital.
"""
import math
import random

def get_exploratory_size(min_usd: float = 5.0, max_usd: float = 10000.0) -> float:
    """
    Generates a log-uniformly distributed trade size between min_usd and max_usd.
    Log-uniform ensures that smaller sizes (e.g., $10-$100) have the same
    probability of being chosen as larger sizes (e.g., $1,000-$10,000).
    
    Rounded to two decimal places.
    """
    if min_usd <= 0:
        min_usd = 1.0
    if max_usd <= min_usd:
        max_usd = min_usd * 10.0
        
    log_min = math.log(min_usd)
    log_max = math.log(max_usd)
    
    # Pick uniform random log value
    random_log = random.uniform(log_min, log_max)
    
    # Exponentiate back to linear scale and round
    size = math.exp(random_log)
    return round(size, 2)
