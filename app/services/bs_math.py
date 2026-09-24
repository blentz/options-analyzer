"""Pure Black-Scholes math: pricing, Greeks, and probability helpers.

Zero dependencies on DB, models, or async code. Everything here is a pure
function on numbers — easy to unit-test, easy to reason about. Higher-level
risk analysis (which mixes BS with positions, scraped IV, scenarios) lives
in app/services/risk.py.

This module is the source of truth for:
  - calculate_option_price (Black-Scholes European call/put)
  - calculate_option_greeks (delta, gamma, theta, vega per-share)
  - _estimate_delta (assignment probability ≈ N(d2))
  - calculate_price_at_delta (inverse: solve for S at target ITM probability)
  - _calculate_price_probability (P(S_T > target) under risk-neutral measure)
  - calculate_touch_probability (P(path hits a barrier before expiry))
  - solve_spot_for_option_price / solve_iv_for_option_price (exact inverses
    of calculate_option_price by bisection)

All angles use natural log/exp. All times are in years (days / 365).
"""

import logging
import math
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


# Constants for Black-Scholes calculations.
# 365 calendar days for time to expiry — industry standard for listed options
# (theta decays over weekends too). 252 trading days is used in some
# academic contexts but not by exchanges.
CALENDAR_DAYS_PER_YEAR = 365.0

# Sourced from app.config.Settings so they can be overridden via env vars
# (RISK_FREE_RATE, DEFAULT_VOLATILITY) without code changes.
DEFAULT_RISK_FREE_RATE = settings.risk_free_rate
DEFAULT_VOLATILITY = settings.default_volatility


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _calculate_d1_d2(
    spot: float,
    strike: float,
    time_years: float,
    risk_free_rate: float,
    volatility: float
) -> tuple[float, float]:
    """
    Calculate Black-Scholes d1 and d2 parameters.
    
    Returns (d1, d2) tuple.
    """
    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * volatility ** 2) * time_years) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    return d1, d2


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def calculate_option_greeks(
    option_type: str,
    spot: float,
    strike: float,
    days_to_expiry: int,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> dict:
    """Compute Black-Scholes Greeks for one option contract.

    Returns delta, gamma, theta (per calendar day), vega (per 1% IV move).
    All values are per-share; the caller multiplies by 100 * contracts and
    by ±1 for short positions to get position Greeks.

    Returns zeros at expiry or for invalid inputs rather than raising —
    callers aggregate across many positions and a single bad one shouldn't
    blow up the whole portfolio Greeks display.
    """
    if days_to_expiry <= 0 or spot <= 0 or strike <= 0 or volatility <= 0:
        # Intrinsic only — delta is 1/-1 if ITM, else 0; other Greeks zero.
        if option_type.upper() == "CALL":
            return {"delta": 1.0 if spot > strike else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        return {"delta": -1.0 if spot < strike else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    sqrt_T = math.sqrt(T)
    try:
        d1, d2 = _calculate_d1_d2(spot, strike, T, risk_free_rate, volatility)
    except (ValueError, ZeroDivisionError):
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * volatility * sqrt_T)
    # Vega per 1% (not per 1 unit) — divide by 100 so callers can use whole-pct IV moves.
    vega = spot * pdf_d1 * sqrt_T / 100.0

    discount = math.exp(-risk_free_rate * T)
    if option_type.upper() == "CALL":
        delta = _norm_cdf(d1)
        # Theta per YEAR; divide by 365 for per-day.
        theta_year = -(spot * pdf_d1 * volatility) / (2 * sqrt_T) - risk_free_rate * strike * discount * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_year = -(spot * pdf_d1 * volatility) / (2 * sqrt_T) + risk_free_rate * strike * discount * _norm_cdf(-d2)
    theta = theta_year / CALENDAR_DAYS_PER_YEAR
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def calculate_option_price(
    option_type: str,
    spot: float,
    strike: float,
    days_to_expiry: float,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
) -> float:
    """
    Calculate theoretical Black-Scholes option price.
    
    Args:
        option_type: "CALL" or "PUT"
        spot: Current underlying price
        strike: Strike price
        days_to_expiry: Days until expiration
        volatility: Implied volatility (annualized, as decimal e.g. 0.30 for 30%)
        risk_free_rate: Risk-free interest rate
    
    Returns:
        Theoretical option price per share
    """
    if days_to_expiry <= 0:
        # At expiry, return intrinsic value
        if option_type.upper() == "CALL":
            return max(0, spot - strike)
        else:
            return max(0, strike - spot)
    
    if spot <= 0 or strike <= 0 or volatility <= 0:
        return 0.0
    
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    
    try:
        d1, d2 = _calculate_d1_d2(spot, strike, T, risk_free_rate, volatility)
        
        if option_type.upper() == "CALL":
            # Call = S * N(d1) - K * e^(-rT) * N(d2)
            price = spot * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * T) * _norm_cdf(d2)
        else:
            # Put = K * e^(-rT) * N(-d2) - S * N(-d1)
            price = strike * math.exp(-risk_free_rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        
        return max(0, price)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _calculate_price_probability(
    current_price: float,
    target_price: float,
    days_to_expiry: int,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    want_above: bool = True
) -> float:
    """
    Calculate probability of underlying reaching target price before expiry.
    
    Uses risk-neutral lognormal distribution from Black-Scholes.
    P(S_T > K) = N(d2) where d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)
    
    Args:
        current_price: Current underlying price
        target_price: Target price to reach
        days_to_expiry: Days until expiration
        volatility: Implied volatility (annualized)
        risk_free_rate: Risk-free interest rate
        want_above: If True, calculates P(S_T > target), else P(S_T < target)
    
    Returns:
        Probability (0 to 1)
    """
    if days_to_expiry <= 0:
        # At expiry, probability is binary
        if want_above:
            return 1.0 if current_price > target_price else 0.0
        else:
            return 1.0 if current_price < target_price else 0.0
    
    if current_price <= 0 or target_price <= 0:
        return 0.5
    
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    
    try:
        # d2 for probability calculation: P(S_T > K) = N(d2)
        # Note: This is different from delta's d1
        _, d2 = _calculate_d1_d2(current_price, target_price, T, risk_free_rate, volatility)
        
        if want_above:
            return _norm_cdf(d2)  # P(S_T > target)
        else:
            return _norm_cdf(-d2)  # P(S_T < target) = 1 - N(d2) = N(-d2)
    except (ValueError, ZeroDivisionError):
        return 0.5


def _estimate_delta(
    option_type: str,
    strike: float,
    current_price: float,
    days_to_expiry: int,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    volatility: float = DEFAULT_VOLATILITY
) -> float:
    """
    Estimate option delta using Black-Scholes formula.
    
    IMPORTANT: Delta is NOT exactly the probability of expiring ITM.
    - Call delta = N(d1), but P(ITM) = N(d2) under risk-neutral measure
    - Put delta = N(d1) - 1, but P(ITM) = N(-d2)
    
    However, delta is commonly used as a rough proxy for ITM probability
    because d1 and d2 are close when volatility*sqrt(T) is small.
    
    For assignment risk estimation, this approximation is acceptable.
    
    Returns:
        float: Absolute delta value (0 to 1) representing approximate ITM probability
    """
    if days_to_expiry <= 0:
        # At expiration, ITM probability is binary
        if option_type == "CALL":
            return 1.0 if current_price > strike else 0.0
        else:
            return 1.0 if current_price < strike else 0.0

    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR

    # Avoid log of zero or negative — surface this rather than silently
    # returning a coin-flip probability that looks like a real answer.
    if current_price <= 0 or strike <= 0 or volatility <= 0:
        logger.warning(
            "_estimate_delta: invalid input S=%s K=%s vol=%s — cannot compute, returning 0.5",
            current_price, strike, volatility,
        )
        return 0.5

    try:
        d1, d2 = _calculate_d1_d2(current_price, strike, T, risk_free_rate, volatility)

        # Return approximate ITM probability using N(d2) for more accuracy
        # N(d2) is the risk-neutral probability of expiring ITM
        if option_type == "CALL":
            return _norm_cdf(d2)  # P(S_T > K) under risk-neutral measure
        else:  # PUT
            return _norm_cdf(-d2)  # P(S_T < K) = 1 - N(d2) = N(-d2)
    except (ValueError, ZeroDivisionError) as e:
        logger.warning(
            "_estimate_delta: BS calc failed for %s K=%.2f S=%.2f dte=%d vol=%.4f: %s — returning 0.5",
            option_type, strike, current_price, days_to_expiry, volatility, e,
        )
        return 0.5


def calculate_price_at_delta(
    option_type: str,
    strike: float,
    days_to_expiry: int,
    target_delta: float = 0.5,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    volatility: float = DEFAULT_VOLATILITY
) -> Optional[float]:
    """
    Calculate the underlying price where assignment probability equals target value.
    
    Uses d2 (not d1) for consistency with _estimate_delta() which calculates
    assignment probability as N(d2) for calls and N(-d2) for puts.
    
    For puts: We want N(-d2) = target_delta, so solve for S where d2 = -norm.ppf(target_delta)
    For calls: We want N(d2) = target_delta, so solve for S where d2 = norm.ppf(target_delta)
    
    Returns the price at which assignment probability equals target_delta.
    """
    if days_to_expiry <= 0:
        # At expiry, the 50% point is exactly at strike
        return strike
    
    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    
    try:
        from scipy.stats import norm
        
        # Solve for S where assignment probability = target_delta
        # Assignment probability uses d2 (not d1):
        #   - Calls: P(ITM) = N(d2), so d2 = norm.ppf(target_delta)
        #   - Puts: P(ITM) = N(-d2), so -d2 = norm.ppf(target_delta), i.e., d2 = -norm.ppf(target_delta)
        
        if option_type == "PUT":
            # For puts: N(-d2) = target_delta, so d2 = -norm.ppf(target_delta)
            d2 = -norm.ppf(target_delta)
        else:
            # For calls: N(d2) = target_delta, so d2 = norm.ppf(target_delta)
            d2 = norm.ppf(target_delta)
        
        # d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)
        # Solving for S:
        # ln(S/K) = d2 * σ√T - (r - σ²/2)T
        # S = K * exp(d2 * σ√T - (r - σ²/2)T)
        sqrt_T = math.sqrt(T)
        exponent = d2 * volatility * sqrt_T - (risk_free_rate - 0.5 * volatility ** 2) * T
        price = strike * math.exp(exponent)
        return price
    except ImportError:
        # Fallback without scipy - use approximation
        # At 50% probability, price is approximately at strike adjusted for drift
        drift_adjustment = math.exp((risk_free_rate - 0.5 * volatility ** 2) * T)
        return strike * drift_adjustment
    except Exception:
        return strike



def calculate_touch_probability(
    current_price: float,
    barrier: float,
    days_to_expiry: float,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """
    Probability the underlying touches `barrier` at any time before expiry.

    Closed-form first-passage probability for GBM under the risk-neutral
    measure. With X_t = ln(S_t/S_0) = mu*t + sigma*W_t, mu = r - sigma^2/2,
    b = ln(H/S_0):
      upper (b > 0): N((mu*T - b)/(sigma*sqrt(T))) + e^(2*mu*b/sigma^2) * N((-b - mu*T)/(sigma*sqrt(T)))
      lower (b < 0): N((b - mu*T)/(sigma*sqrt(T))) + e^(2*mu*b/sigma^2) * N((b + mu*T)/(sigma*sqrt(T)))

    A resting limit order fills on a touch, not on the expiry close, so this
    is the relevant number for "will my buy-to-close order fill". It is
    always >= the terminal probability from _calculate_price_probability.
    """
    if current_price <= 0 or barrier <= 0:
        return 0.0
    if barrier == current_price:
        return 1.0
    if days_to_expiry <= 0 or volatility <= 0:
        return 0.0

    T = days_to_expiry / CALENDAR_DAYS_PER_YEAR
    sig_sqrt_t = volatility * math.sqrt(T)
    mu = risk_free_rate - 0.5 * volatility ** 2
    b = math.log(barrier / current_price)
    reflection = math.exp(2.0 * mu * b / volatility ** 2)
    if b > 0:
        p = _norm_cdf((mu * T - b) / sig_sqrt_t) + reflection * _norm_cdf((-b - mu * T) / sig_sqrt_t)
    else:
        p = _norm_cdf((b - mu * T) / sig_sqrt_t) + reflection * _norm_cdf((b + mu * T) / sig_sqrt_t)
    return min(1.0, max(0.0, p))


def _bisect(f, lo: float, hi: float, tol: float = 1e-7, max_iter: int = 200) -> Optional[float]:
    """Root of monotonic f on [lo, hi], or None when f(lo), f(hi) share a sign."""
    f_lo, f_hi = f(lo), f(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = f(mid)
        if f_mid == 0 or (hi - lo) / 2 < tol:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return (lo + hi) / 2


def solve_spot_for_option_price(
    option_type: str,
    strike: float,
    target_price: float,
    days_to_expiry: float,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> Optional[float]:
    """
    Underlying price at which the Black-Scholes value equals `target_price`.

    Exact inverse of calculate_option_price in the spot dimension. Replaces
    the delta-linear estimate, which ignores gamma and under-states the
    move needed for cheap OTM targets (HITI $2.50P, $0.10 -> $0.05: linear
    says $2.81, exact says $2.93).

    Returns None when no spot produces the target (target <= 0, or a put
    target at/above the discounted strike).
    """
    if target_price <= 0 or strike <= 0:
        return None
    is_call = option_type.upper() == "CALL"
    if days_to_expiry <= 0:
        return strike + target_price if is_call else (strike - target_price if target_price < strike else None)

    # Price is monotonic in spot: increasing for calls, decreasing for puts.
    def f(s: float) -> float:
        return calculate_option_price(option_type, s, strike, days_to_expiry, volatility, risk_free_rate) - target_price

    return _bisect(f, strike * 1e-4, strike * 50, tol=strike * 1e-8)


def solve_iv_for_option_price(
    option_type: str,
    spot: float,
    strike: float,
    days_to_expiry: float,
    option_price: float,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> Optional[float]:
    """
    Implied volatility that makes the Black-Scholes value equal `option_price`.

    Used to back IV out of a live mid so the model is anchored to the price
    the position can actually be traded at. Returns None at expiry, or when
    the price is outside the no-arbitrage range for any IV in [0.1%, 1000%].
    """
    if days_to_expiry <= 0 or option_price <= 0 or spot <= 0 or strike <= 0:
        return None

    def f(v: float) -> float:
        return calculate_option_price(option_type, spot, strike, days_to_expiry, v, risk_free_rate) - option_price

    return _bisect(f, 0.001, 10.0)
