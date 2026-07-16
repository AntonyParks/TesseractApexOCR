"""Glicko-2 rating math (Glickman 2013), pure functions — no I/O, no DB.

Used by elo_engine to replace the old sum-of-pairwise K-factor ELO. Each player carries three
numbers on the ORIGINAL (1000-centered) scale: rating (mu), deviation (rd, "how unsure we are"),
and volatility (vol). One match = one Glicko-2 "rating period": every pairwise-survival result in
that match is pooled into a SINGLE update. This is what makes swing magnitude depend on opponent
STRENGTH (the expected-score term) rather than opponent COUNT — more opponents observed shrinks rd
(more certainty) instead of inflating the rating jump. The leaderboard ranks by the conservative
estimate mu - 2*rd, so unproven players (large rd) sit low until they earn certainty.

Scale: we keep the display scale 1000-centered (RATING0=1000) instead of Glicko's usual 1500, so
values read like the ELO numbers the project already uses. Internally we convert to the Glicko-2
scale (divide by SCALE=173.7178) for the update, then convert back.

Reference: http://www.glicko.net/glicko/glicko2.pdf
"""
import math

RATING0 = 1000.0        # starting rating (mu) for an unrated player
RD0 = 350.0             # starting deviation — maximally uncertain
VOL0 = 0.06             # starting volatility
SCALE = 173.7178        # Glicko-2 <-> display-scale conversion constant
TAU = 0.5               # system constant: constrains volatility change (0.3-1.2 typical)
EPSILON = 1e-6          # convergence tolerance for the volatility solver

# Cap on the EFFECTIVE number of opponents a single match contributes. Without it, standard
# Glicko-2 lets opponent COUNT dominate opponent STRENGTH (a match revealing 48 weak deaths
# out-swings one revealing 5 Predator deaths), which is an observation artifact and the opposite
# of what we want (decision #2). When a match has more than this many results, every result is
# down-weighted as a fractional game (w = cap/N) so the match's rating SWING is governed by who
# you beat, while extra opponents still shrink rd (more certainty). At/below the cap the update is
# exact standard Glicko-2. Tuned so 5 Preds out-swings 48 Golds while 48 Golds still adds more
# certainty (measured 2026-07-16).
MAX_EFFECTIVE_OPPONENTS = 8

# rd never shrinks below this (a fully-converged player can still move a little) and the
# conservative-rank penalty stays meaningful. Also the "provisional" display cutoff lives above it.
RD_FLOOR = 30.0


def default_state() -> dict:
    """A fresh, maximally-uncertain rating."""
    return {"rating": RATING0, "rd": RD0, "vol": VOL0}


def conservative(rating: float, rd: float) -> float:
    """Leaderboard-ranking value: 95% lower bound on true skill (mu - 2*rd)."""
    return rating - 2.0 * rd


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _E(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def update(rating: float, rd: float, vol: float, results: list) -> dict:
    """Run one Glicko-2 rating period.

    results: list of (opp_rating, opp_rd, score) on the DISPLAY scale, score in {0.0, 1.0}
             (1.0 = this player outlasted the opponent, 0.0 = outlasted-by).
    Returns a new {"rating", "rd", "vol"} dict. Empty results => rating/vol unchanged, rd
    left as-is (we do NOT inflate rd for idle periods in this rebuild-oriented engine).
    """
    if not results:
        return {"rating": rating, "rd": max(rd, RD_FLOOR), "vol": vol}

    # Down-weight results as fractional games when a match reveals many opponents, so opponent
    # STRENGTH (not count) drives the swing; extra opponents still add certainty (see the constant).
    n = len(results)
    w = 1.0 if n <= MAX_EFFECTIVE_OPPONENTS else MAX_EFFECTIVE_OPPONENTS / n

    # Step 2: to Glicko-2 scale
    mu = (rating - RATING0) / SCALE
    phi = rd / SCALE

    opp = [((r - RATING0) / SCALE, d / SCALE, s) for (r, d, s) in results]

    # Step 3: estimated variance v (weighted)
    v_inv = 0.0
    for mu_j, phi_j, _s in opp:
        gj = _g(phi_j)
        ej = _E(mu, mu_j, phi_j)
        v_inv += w * gj * gj * ej * (1.0 - ej)
    if v_inv <= 0.0:
        return {"rating": rating, "rd": max(rd, RD_FLOOR), "vol": vol}
    v = 1.0 / v_inv

    # Step 4: estimated improvement delta (weighted)
    delta_sum = 0.0
    for mu_j, phi_j, s in opp:
        delta_sum += w * _g(phi_j) * (s - _E(mu, mu_j, phi_j))
    delta = v * delta_sum

    # Step 5: new volatility via Illinois (regula falsi) root-finding
    a = math.log(vol * vol)
    phi2 = phi * phi

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi2 - v - ex)
        den = 2.0 * (phi2 + v + ex) ** 2
        return num / den - (x - a) / (TAU * TAU)

    A = a
    if delta * delta > phi2 + v:
        B = math.log(delta * delta - phi2 - v)
    else:
        k = 1
        while f(a - k * TAU) < 0.0:
            k += 1
        B = a - k * TAU

    fA, fB = f(A), f(B)
    while abs(B - A) > EPSILON:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0.0:
            A, fA = B, fB
        else:
            fA /= 2.0
        B, fB = C, fC
    new_vol = math.exp(A / 2.0)

    # Step 6-7: new deviation and rating
    phi_star = math.sqrt(phi2 + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * delta_sum

    # Step 8: back to display scale
    new_rating = SCALE * new_mu + RATING0
    new_rd = max(RD_FLOOR, SCALE * new_phi)
    return {"rating": new_rating, "rd": new_rd, "vol": new_vol}
