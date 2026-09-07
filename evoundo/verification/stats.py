"""Statistical functions and Wilson score lower confidence bounds for EvoUndo Harness."""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Any, Dict


def wilson_score_lower_bound(k: int, n: int, confidence: float = 0.95) -> float:
    """Calculate the Wilson score lower confidence bound for a binomial proportion.
    
    Given k successes out of n independent Bernoulli trials:
      p_hat = k / n
      z = quantile of standard normal distribution corresponding to (1 + confidence) / 2
      LCB = (p_hat + z^2 / (2n) - z * sqrt((p_hat * (1 - p_hat) + z^2 / (4n)) / n)) / (1 + z^2 / n)
    
    For n = 0 or k = 0, returns 0.0.
    """
    if n <= 0 or k <= 0:
        return 0.0

    # Common z-values for standard confidence intervals
    if abs(confidence - 0.95) < 1e-4:
        z = 1.959963984540054
    elif abs(confidence - 0.90) < 1e-4:
        z = 1.6448536269514722
    elif abs(confidence - 0.99) < 1e-4:
        z = 2.5758293035489004
    else:
        alpha = 1.0 - confidence
        t = math.sqrt(-2.0 * math.log(alpha / 2.0))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        z = t - ((c2 * t + c1) * t + c0) / (((d3 * t + d2) * t + d1) * t + 1.0)

    p_hat = k / n
    denominator = 1.0 + (z ** 2) / n
    center_adjusted = p_hat + (z ** 2) / (2.0 * n)
    adjusted_std_dev = math.sqrt((p_hat * (1.0 - p_hat) + (z ** 2) / (4.0 * n)) / n)
    
    lcb = (center_adjusted - z * adjusted_std_dev) / denominator
    return max(0.0, min(1.0, lcb))


@dataclass
class WilsonScoreReport:
    """Detailed statistical report of verification trial success rates and confidence intervals."""
    successes: int
    trials: int
    empirical_rate: float
    wilson_lcb: float
    confidence: float = 0.95

    @classmethod
    def compute(cls, successes: int, trials: int, confidence: float = 0.95) -> WilsonScoreReport:
        rate = successes / trials if trials > 0 else 0.0
        lcb = wilson_score_lower_bound(successes, trials, confidence=confidence)
        return cls(
            successes=successes,
            trials=trials,
            empirical_rate=rate,
            wilson_lcb=lcb,
            confidence=confidence,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "successes": self.successes,
            "trials": self.trials,
            "empirical_rate": round(self.empirical_rate, 4),
            "wilson_lcb": round(self.wilson_lcb, 6),
            "confidence": self.confidence,
        }
