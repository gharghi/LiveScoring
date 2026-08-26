"""Optimised route through the cylinders.  [algorithm C]

THE HIGHEST-RISK COMPONENT IN THE SYSTEM. On the reference task, centre-to-
centre is 80.80 km and the optimised route is 59.65 km. Every distance-based
scoring quantity — distance points, distance to goal, the ranking of everyone
who did not reach goal, the leading coefficient — is derived from it. A 1%
error here moves more points than most rules do.

It has been wrong twice, both times plausibly:

  * 8.3% long, because each point was placed on its cylinder toward the
    MIDPOINT of its neighbours. That is the correct minimiser only when the
    two legs are equal length; the actual condition on a circle is the
    reflection law. (VERIFICATION.md §4.1)

  * still 2.0 km long after that, because coordinate descent stops at the
    first arrangement no SINGLE point can improve. Worse, the obvious test —
    perturb each point and check nothing shorter exists — PASSED on it, because
    the test moved one point at a time too. It shared the bug's assumption.

Everything below is shaped by the second one.

--------------------------------------------------------------------------
THE INTERFACE
--------------------------------------------------------------------------
Deliberately plain: a route is a list of `(x, y, radius)` in projected metres,
and `first` is the index the SCORED route starts at. No task object, no
waypoint class, nothing to mock. Every function here can be called with three
tuples and checked by hand.

    optimise_route(pts, first)     -> (px, py)   the driver; use this
      best_point_in_disc(...)      -> (x, y)     exact 1-D minimiser, one point
      shortest_route_dp(...)       -> (px, py)   discretised global search
      polish_route(...)            -> (px, py)   coordinate descent
      route_length(px, py, first)  -> metres
      leg_lengths / remaining_table                the per-waypoint tables

--------------------------------------------------------------------------
WHY THREE SEEDS
--------------------------------------------------------------------------
`optimise_route` polishes THREE starting arrangements and keeps the shortest:
every point at its cylinder centre, plus two discretised shortest-path
solutions at different resolutions. Coordinate descent alone converges to a
local minimum and reports itself finished; the DP cannot get stuck, because it
considers every combination of sample points at once, and its answer is a real
feasible route so polishing can only improve it.

Costs about 20 ms, once, per task. Speed here is irrelevant and accuracy is
everything — this runs at task-compile time, never per position.

--------------------------------------------------------------------------
A POINT MAY SIT INSIDE ITS CYLINDER
--------------------------------------------------------------------------
Not only on the rim. When the direct line between two neighbours already passes
through a large cylinder, the detour is ZERO and forcing the point onto the rim
invents distance nobody flew. `best_point_in_disc` checks the segment first.

--------------------------------------------------------------------------
STILL 0.42% SHORT OF THE PUBLISHED RESULT
--------------------------------------------------------------------------
59,647 m against an official 59,900 m. The route below is a verified local AND
global optimum for the geometry as modelled, so this is a MODELLING question,
not an optimiser bug. Three candidates, unseparated (VERIFICATION.md §5.4):
the earth model (WGS84 is 0.18% longer — see rules/earth_model.py), the
concentric start cylinder (this engine collapses that leg to zero; pinning the
start to the rim adds 1.0 km), or the official's own optimiser being above the
true optimum.

VERIFIED: `--verify` checks the result two independent ways — a perturbation
search over every point's own cylinder (rim and interior), and a shortest-path
DP at a resolution `optimise_route` does not itself use, so it is a cross-check
and not a restatement.
"""

from __future__ import annotations

import math

_GOLDEN = (math.sqrt(5.0) - 1.0) / 2.0


# --- one point ------------------------------------------------------------


def best_point_in_disc(ax, ay, bx, by, cx, cy, r, two_legs=True):
    """The point of the disc (c, r) minimising |A-P| + |P-B|.

    Three cases, in order:

      * ONE LEG ONLY (`two_legs=False`) — the SSS has nothing before it and
        goal has nothing after it. The answer is the point of the disc closest
        to the single neighbour, which is the neighbour itself when it is
        already inside.

      * THE SEGMENT A-B ALREADY CROSSES THE DISC — the minimum is |A-B| and
        any point of the intersection attains it. The closest point of the
        segment to the centre is picked, which keeps the iteration stable.

      * OTHERWISE the optimum is on the rim, where the correct condition is the
        REFLECTION LAW: (P-A)/|P-A| + (P-B)/|P-B| parallel to (P-c). That is
        NOT "the rim point nearest the midpoint of A and B" — those coincide
        only when |A-P| == |B-P|, which is why the midpoint heuristic looks
        right on symmetric legs and silently drifts on real tasks. Minimised
        directly, by golden-section search on the rim angle.
    """
    if not two_legs:
        dx, dy = ax - cx, ay - cy
        d = math.hypot(dx, dy)
        if d <= r:
            return ax, ay
        return cx + dx / d * r, cy + dy / d * r

    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 > 1e-18:
        u = ((cx - ax) * dx + (cy - ay) * dy) / l2
        u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)
        qx, qy = ax + u * dx, ay + u * dy
        if math.hypot(qx - cx, qy - cy) <= r:
            return qx, qy

    def f(theta):
        x = cx + math.cos(theta) * r
        y = cy + math.sin(theta) * r
        return math.hypot(x - ax, y - ay) + math.hypot(x - bx, y - by)

    # Coarse sweep to bracket. f has one minimum on the rim, but the sweep
    # costs nothing here and removes any dependence on that being true.
    best_t, best_v = 0.0, float("inf")
    STEPS = 72
    for k in range(STEPS):
        t = k * 2.0 * math.pi / STEPS
        v = f(t)
        if v < best_v:
            best_t, best_v = t, v
    step = 2.0 * math.pi / STEPS
    lo, hi = best_t - step, best_t + step

    # 60 golden-section iterations take a 0.09 rad bracket below float
    # precision, so the rim point is exact at any radius.
    c1 = hi - _GOLDEN * (hi - lo)
    c2 = lo + _GOLDEN * (hi - lo)
    f1, f2 = f(c1), f(c2)
    for _ in range(60):
        if f1 < f2:
            hi, c2, f2 = c2, c1, f1
            c1 = hi - _GOLDEN * (hi - lo)
            f1 = f(c1)
        else:
            lo, c1, f1 = c1, c2, f2
            c2 = lo + _GOLDEN * (hi - lo)
            f2 = f(c2)
    t = (lo + hi) * 0.5
    return cx + math.cos(t) * r, cy + math.sin(t) * r


# --- the whole route ------------------------------------------------------


def route_length(px, py, first: int) -> float:
    """Total length of the polyline from index `first` to the last point."""
    return sum(math.hypot(px[i + 1] - px[i], py[i + 1] - py[i])
               for i in range(first, len(px) - 1))


def shortest_route_dp(pts, first: int, k: int, fracs):
    """Shortest route through a DISCRETISED version of the cylinders.

    `pts` is [(x, y, radius)]. Each cylinder is sampled at `k` angles on each
    of `fracs` radii, plus its centre; an exact shortest-path DP then runs over
    the layers. It cannot get stuck in a local minimum — it considers every
    combination of sample points — and its answer is a real feasible route, so
    it is an upper bound that polishing can only improve.

    This is what escapes the coordinate-descent trap described at the top.
    """
    n = len(pts)

    def samples(p):
        x, y, r = p
        out = [(x, y)]
        for fr in fracs:
            for j in range(k):
                a = 2.0 * math.pi * j / k
                out.append((x + math.cos(a) * r * fr, y + math.sin(a) * r * fr))
        return out

    layers = [samples(pts[i]) for i in range(first, n)]
    cost = [0.0] * len(layers[0])
    backs = []
    for li in range(1, len(layers)):
        prev, cur = layers[li - 1], layers[li]
        nc, bk = [], []
        for bx, by in cur:
            best, bi = 1e18, 0
            for i, ((ax, ay), c) in enumerate(zip(prev, cost)):
                v = c + math.hypot(bx - ax, by - ay)
                if v < best:
                    best, bi = v, i
            nc.append(best)
            bk.append(bi)
        cost = nc
        backs.append(bk)
    j = min(range(len(cost)), key=cost.__getitem__)
    path = [j]
    for bk in reversed(backs):
        j = bk[j]
        path.append(j)
    path.reverse()

    px = [p[0] for p in pts]
    py = [p[1] for p in pts]
    for off, idx in enumerate(path):
        px[first + off], py[first + off] = layers[off][idx]
    return px, py


def polish_route(pts, px, py, first: int, iterations: int = 500,
                 eps: float = 1e-4):
    """Coordinate descent: replace each point by the exact minimiser of its legs.

    Each sweep can only shorten the route, so the iteration is monotone and
    terminates. It CANNOT escape a local minimum — that is what the DP seed is
    for.
    """
    n = len(pts)
    px, py = list(px), list(py)
    for _ in range(iterations):
        moved = 0.0
        for i in range(first, n):
            x, y, r = pts[i]
            if i == first:
                if i + 1 >= n:
                    continue
                # Nothing before the scored route's first point: minimise the
                # distance to what FOLLOWS. Pulling it toward the takeoff, as a
                # symmetric treatment would, lengthens the task for everybody.
                nx, ny = best_point_in_disc(px[i + 1], py[i + 1], 0.0, 0.0,
                                            x, y, r, two_legs=False)
            elif i + 1 < n:
                nx, ny = best_point_in_disc(px[i - 1], py[i - 1],
                                            px[i + 1], py[i + 1], x, y, r)
            else:
                # Goal: nothing after it, so the nearest point of the cylinder.
                nx, ny = best_point_in_disc(px[i - 1], py[i - 1], 0.0, 0.0,
                                            x, y, r, two_legs=False)
            d = math.hypot(nx - px[i], ny - py[i])
            if d > moved:
                moved = d
            px[i], py[i] = nx, ny
        if moved < eps:
            break
    return px, py


def split_absorbed_points(pts, px, py, first: int, eps: float = 1e-6):
    """Put every optimised point back ON its own cylinder boundary, in place.

    THE TOTAL ROUTE LENGTH DOES NOT CHANGE. Only the boundary between two legs
    moves, and it moves to where the pilot actually crosses the cylinder.

    Why it is needed: when cylinder i+1 lies inside cylinder i, the shortest
    polyline is free to put p_i anywhere in the overlap — including exactly on
    top of p_{i+1}, which is what coordinate descent does, because it costs
    nothing. The path is right and the SPLIT is wrong.

    On the samples task the ESS is a 2,000 m cylinder and goal a 1,000 m
    cylinder at the SAME centre. The optimiser put the ESS point at 1,000 m,
    on top of goal, so the ESS→goal leg came out 0.000 km and the SPEED SECTION
    swallowed the last kilometre — the section the clock runs on, overstated by
    1 km on a 71 km task.

    The fix: if p_i sits strictly inside its own cylinder, slide it back along
    the incoming leg to where that leg first crosses the boundary. It stays on
    the same straight line, so nothing about the route changes; the ESS is now
    recorded where the pilot crosses it.
    """
    n = len(pts)
    px, py = list(px), list(py)
    for i in range(first + 1, n):
        x, y, r = pts[i]
        if r <= 0.0:
            continue
        d = math.hypot(px[i] - x, py[i] - y)
        if d >= r - eps:
            continue                       # already on (or outside) the rim
        ax, ay = px[i - 1], py[i - 1]
        if math.hypot(ax - x, ay - y) <= r:
            continue                       # incoming point is inside too
        # First crossing of the circle along p_{i-1} -> p_i, extended.
        dx, dy = px[i] - ax, py[i] - ay
        fx, fy = ax - x, ay - y
        a = dx * dx + dy * dy
        if a < 1e-18:
            continue
        b = 2.0 * (fx * dx + fy * dy)
        c = fx * fx + fy * fy - r * r
        disc = b * b - 4 * a * c
        if disc < 0.0:
            continue
        sq = math.sqrt(disc)
        t = (-b - sq) / (2 * a)            # the FIRST intersection
        if not (0.0 <= t <= 1.0):
            continue
        px[i], py[i] = ax + t * dx, ay + t * dy
    return px, py


def optimise_route(pts, first: int, iterations: int = 500, eps: float = 1e-4):
    """Shortest polyline visiting each cylinder in order. THE ENTRY POINT.

    `pts` is [(x, y, radius)] in projected metres; `first` is where the SCORED
    route begins. Returns (px, py), one optimised point per input point.

    Three seeds, each polished to its own local minimum, shortest wins.
    """
    n = len(pts)
    seeds = [([p[0] for p in pts], [p[1] for p in pts])]     # all centred
    if first + 1 < n:
        seeds.append(shortest_route_dp(pts, first, 32, (1.0, 0.5)))
        seeds.append(shortest_route_dp(pts, first, 96, (1.0, 0.66, 0.33)))

    best, best_len = None, float("inf")
    for sx, sy in seeds:
        cx, cy = polish_route(pts, sx, sy, first, iterations, eps)
        cl = route_length(cx, cy, first)
        if cl < best_len:
            best, best_len = (cx, cy), cl
    # Same route, correct leg boundaries. See split_absorbed_points().
    return split_absorbed_points(pts, best[0], best[1], first)


# --- the per-waypoint distance tables -------------------------------------


def leg_lengths(px, py, first: int) -> list[float]:
    """Length of each leg i -> i+1. Zero before `first`, which is off-route."""
    n = len(px)
    legs = [0.0] * n
    for i in range(first, n - 1):
        legs[i] = math.hypot(px[i + 1] - px[i], py[i + 1] - py[i])
    return legs


def remaining_table(legs: list[float]) -> list[float]:
    """remaining[i] = optimised distance from point i onward to goal.

    Built once at compile time; the hot path then measures a pilot's distance
    still to fly as `edge distance to the next un-tagged cylinder` plus one
    lookup here. That is what keeps the per-fix cost to a single square root.
    """
    n = len(legs)
    remaining = [0.0] * n
    acc = 0.0
    for i in range(n - 2, -1, -1):
        acc += legs[i]
        remaining[i] = acc
    return remaining
