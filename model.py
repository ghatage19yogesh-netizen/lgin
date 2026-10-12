"""
Simplified COM allocation model.

Mirrors the structure of the production pipeline (optimization.py + alloc_solver_v2.py):
  1. Classify products (ok / low velocity / no demand / restricted)
  2. Place special products with business rules
  3. Reduce warehouse space by what the rules used
  4. Solve an LP for the remaining products: minimise shipping cost + overflow penalty
     subject to demand, min/max warehouse share, storage capacity (soft), exceptions
     and the medium-velocity 2-building limit
  5. Combine everything into one plan

Uses scipy's HiGHS solver instead of Gurobi so it runs anywhere (e.g. Streamlit Cloud).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import linprog

# --------------------------------------------------------------------------------------
# Sample data
# --------------------------------------------------------------------------------------
WAREHOUSES = ["CFC 1 · East", "CFC 2 · Central", "CFC 3 · West"]
ZONES = ["East", "Central", "West"]
STORAGE_TYPES = ["FLA", "HFG"]

# Base shipping cost per unit (warehouse -> zone). Products scale it by cost_factor.
BASE_COST = pd.DataFrame(
    [[2.0, 3.5, 5.0],
     [3.5, 2.0, 3.5],
     [5.0, 3.5, 2.0]],
    index=WAREHOUSES, columns=ZONES,
)

# Lanes with no cost data in the sample (to show the "missing cost" warning).
MISSING_COST_LANES = [("CFC 3 · West", "East", "305-410-high")]


def sample_products() -> pd.DataFrame:
    rows = [
        # product_type, description, storage, velocity, E, C, W, order_qty, rule, rule_cfcs, allowed_cfcs, cost_factor
        ("101-210-high",   "Men's shirts",        "FLA", "high",   600, 300, 400,  0, "",            "", "", 1.00),
        ("101-215-high",   "Women's tops",        "FLA", "high",   450, 350, 500,  0, "",            "", "", 0.90),
        ("202-330-medium", "Dresses",             "HFG", "medium", 200, 150, 300,  0, "",            "", "CFC 1 · East, CFC 3 · West", 1.20),
        ("202-340-medium", "Blazers",             "HFG", "medium", 120, 180,  90,  0, "",            "", "CFC 1 · East, CFC 2 · Central", 1.30),
        ("305-410-high",   "Coats",               "HFG", "high",   250, 200, 150,  0, "forbidden_in", "CFC 2 · Central", "", 1.50),
        ("410-520-low",    "Silk scarves",        "FLA", "low",     40,  35,  25,  0, "",            "", "", 0.60),
        ("415-525-low",    "Formal gowns",        "HFG", "low",     20,  10,  30,  0, "",            "", "", 1.40),
        ("520-610-high",   "New brand socks",     "FLA", "high",     0,   0,   0, 60, "",            "", "", 0.50),
        ("610-700-high",   "Fine jewellery",      "FLA", "high",    80,  60,  70,  0, "must_be_in",  "CFC 1 · East", "", 0.40),
    ]
    cols = ["product_type", "description", "storage", "velocity", "demand_East", "demand_Central",
            "demand_West", "order_qty", "rule", "rule_cfcs", "allowed_cfcs", "cost_factor"]
    return pd.DataFrame(rows, columns=cols)


def sample_capacity() -> pd.DataFrame:
    return pd.DataFrame(
        {"FLA": [2000, 1500, 1800], "HFG": [700, 450, 500]},
        index=pd.Index(WAREHOUSES, name="Warehouse"),
    )


def sample_limits() -> pd.DataFrame:
    return pd.DataFrame(
        {"min_pct": [25.0, 20.0, 25.0], "max_pct": [50.0, 40.0, 45.0]},
        index=pd.Index(WAREHOUSES, name="Warehouse"),
    )


@dataclass
class Settings:
    overflow_penalty: float = 50.0      # $ per unit over capacity
    time_limit_s: float = 60.0
    slow_tie_break: str = "Lowest cost"  # "Lowest cost" | "Keep last month's" | "Random"
    count_rule_stock_in_shares: bool = True
    missing_cost_fill: str = "Warehouse average"  # "Warehouse average" | "Minimum cost (legacy)"
    random_seed: int | None = None


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def _split(s: str) -> list[str]:
    return [x.strip() for x in str(s or "").split(",") if x.strip()]


def demand_by_zone(row) -> dict[str, float]:
    return {z: float(row[f"demand_{z}"]) for z in ZONES}


def build_costs(products: pd.DataFrame, settings: Settings, min_cost: float = 0.01):
    """Return {(w, z, p): cost} and the list of lanes that were filled in."""
    costs, filled = {}, []
    missing = set(MISSING_COST_LANES)
    for _, r in products.iterrows():
        p = r["product_type"]
        for w in WAREHOUSES:
            known = {z: BASE_COST.loc[w, z] * r["cost_factor"] for z in ZONES if (w, z, p) not in missing}
            for z in ZONES:
                if z in known:
                    costs[(w, z, p)] = known[z]
                else:
                    if settings.missing_cost_fill.startswith("Minimum"):
                        costs[(w, z, p)] = min_cost
                    else:
                        costs[(w, z, p)] = float(np.mean(list(known.values())))
                    filled.append({"warehouse": w, "zone": z, "product_type": p,
                                   "filled_cost": round(costs[(w, z, p)], 2)})
    return costs, filled


# --------------------------------------------------------------------------------------
# Validation (pre-run checks)
# --------------------------------------------------------------------------------------
def validate(products: pd.DataFrame, capacity: pd.DataFrame, limits: pd.DataFrame) -> list[dict]:
    checks = []
    bad = limits[limits["min_pct"] > limits["max_pct"]]
    checks.append({"level": "error" if len(bad) else "ok",
                   "text": "Each warehouse min ≤ max" + (f" — fails for {', '.join(bad.index)}" if len(bad) else "")})
    smax, smin = limits["max_pct"].sum(), limits["min_pct"].sum()
    checks.append({"level": "ok" if smax >= 100 else "error",
                   "text": f"Max shares add up to ≥ 100% (now {smax:.0f}%)"})
    checks.append({"level": "ok" if smin <= 100 else "error",
                   "text": f"Min shares add up to ≤ 100% (now {smin:.0f}%)"})
    if (capacity < 0).any().any():
        checks.append({"level": "error", "text": "Capacity cannot be negative"})
    dem_cols = [f"demand_{z}" for z in ZONES]
    if (products[dem_cols] < 0).any().any():
        checks.append({"level": "error", "text": "Demand cannot be negative"})
    checks.append({"level": "warn" if MISSING_COST_LANES else "ok",
                   "text": f"{len(MISSING_COST_LANES)} shipping cost lane(s) missing — will be filled"})
    total = products[dem_cols].sum(axis=1) + products["order_qty"]
    for k in STORAGE_TYPES:
        need = total[products["storage"] == k].sum()
        have = capacity[k].sum()
        if need > have:
            checks.append({"level": "warn",
                           "text": f"{k} demand ({need:,.0f}) is more than total {k} space ({have:,.0f}) — overflow will be penalised"})
    nd = int(((products[dem_cols].sum(axis=1) == 0) & (products["order_qty"] > 0)).sum())
    if nd:
        checks.append({"level": "info", "text": f"{nd} product(s) without demand — split evenly"})
    return checks


# --------------------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------------------
def run_allocation(products: pd.DataFrame, capacity: pd.DataFrame, limits: pd.DataFrame,
                   settings: Settings, previous_slow_map: dict | None = None,
                   progress=None) -> dict:
    t0 = time.time()
    say = progress or (lambda msg: None)
    products = products.copy().reset_index(drop=True)
    capacity = capacity.astype(float).copy()
    limits = limits.astype(float).copy()
    previous_slow_map = previous_slow_map or {}
    rng = np.random.default_rng(settings.random_seed)

    say("Loading data and filling missing costs")
    costs, filled_lanes = build_costs(products, settings)

    # ---- 1. classify -----------------------------------------------------------------
    say("Sorting products into groups")
    dem_cols = [f"demand_{z}" for z in ZONES]
    tot = products[dem_cols].sum(axis=1)
    groups = {}
    for i, r in products.iterrows():
        if tot[i] == 0 and r["order_qty"] > 0:
            groups[r["product_type"]] = "No demand"
        elif r["rule"] == "must_be_in":
            groups[r["product_type"]] = "Restricted"
        elif r["velocity"] == "low":
            groups[r["product_type"]] = "Slow seller"
        else:
            groups[r["product_type"]] = "Optimizer"

    # ---- 2. rule-based placements ----------------------------------------------------
    say("Placing special products with business rules")
    manual_rows, slow_map, slow_notes = [], {}, []
    for _, r in products.iterrows():
        p, g = r["product_type"], groups[r["product_type"]]
        forbidden = _split(r["rule_cfcs"]) if r["rule"] == "forbidden_in" else []
        allowed = [w for w in WAREHOUSES if w not in forbidden]
        if g == "No demand":
            share = r["order_qty"] / len(allowed)
            for w in allowed:
                manual_rows.append(dict(warehouse=w, zone="(no demand)", product_type=p, storage=r["storage"],
                                        units=share, unit_cost=np.nan, cost=0.0, method="No demand"))
        elif g == "Restricted":
            w = _split(r["rule_cfcs"])[0]
            for z, d in demand_by_zone(r).items():
                if d > 0:
                    c = costs[(w, z, p)]
                    manual_rows.append(dict(warehouse=w, zone=z, product_type=p, storage=r["storage"],
                                            units=d, unit_cost=c, cost=c * d, method="Restricted"))
        elif g == "Slow seller":
            dz = demand_by_zone(r)
            wcost = {w: sum(costs[(w, z, p)] * d for z, d in dz.items()) for w in allowed}
            best = min(wcost, key=wcost.get)
            ties = [w for w in allowed if abs(wcost[w] - wcost[best]) < 1e-9]
            if settings.slow_tie_break == "Random":
                w = str(rng.choice(allowed))
                note = "Random pick"
            elif settings.slow_tie_break == "Keep last month's" and previous_slow_map.get(p) in allowed:
                w = previous_slow_map[p]
                note = "Kept last month's warehouse"
            else:
                w = ties[0]
                note = "Lowest cost" + (" (tie)" if len(ties) > 1 else "")
            slow_map[p] = w
            prev = previous_slow_map.get(p)
            slow_notes.append(dict(product_type=p, warehouse=w, how=note,
                                   last_month=prev or "—", changed=bool(prev and prev != w)))
            for z, d in dz.items():
                if d > 0:
                    c = costs[(w, z, p)]
                    manual_rows.append(dict(warehouse=w, zone=z, product_type=p, storage=r["storage"],
                                            units=d, unit_cost=c, cost=c * d, method="Slow seller"))
    manual = pd.DataFrame(manual_rows, columns=["warehouse", "zone", "product_type", "storage",
                                                "units", "unit_cost", "cost", "method"])

    # ---- 3. update capacity ----------------------------------------------------------
    say("Updating warehouse space")
    used_manual = manual.groupby(["warehouse", "storage"])["units"].sum() if len(manual) else pd.Series(dtype=float)
    rem_cap = capacity.copy()
    for (w, k), u in used_manual.items():
        rem_cap.loc[w, k] -= u
    manual_overflow = (-rem_cap.clip(upper=0)).copy()
    rem_cap = rem_cap.clip(lower=0)

    # ---- 4. LP -------------------------------------------------------------------------
    say("Building the optimisation model")
    ok = products[products["product_type"].map(groups) == "Optimizer"].reset_index(drop=True)
    W, Z, P, K = len(WAREHOUSES), len(ZONES), len(ok), len(STORAGE_TYPES)
    nx = W * Z * P
    npen = W * K
    xi = lambda i, j, p: (i * Z + j) * P + p
    pi = lambda i, k: nx + i * K + k

    c = np.zeros(nx + npen)
    bounds = [(0, None)] * (nx + npen)
    for p, r in ok.iterrows():
        forbidden = _split(r["rule_cfcs"]) if r["rule"] == "forbidden_in" else []
        allowed_med = _split(r["allowed_cfcs"]) if r["velocity"] == "medium" else []
        for i, w in enumerate(WAREHOUSES):
            blocked = (w in forbidden) or (allowed_med and w not in allowed_med)
            for j, z in enumerate(ZONES):
                c[xi(i, j, p)] = costs[(w, z, r["product_type"])]
                if blocked:
                    bounds[xi(i, j, p)] = (0, 0)
    for i in range(W):
        for k in range(K):
            c[pi(i, k)] = settings.overflow_penalty

    A, b = [], []
    # demand: sum_i x >= d
    for p, r in ok.iterrows():
        for j, z in enumerate(ZONES):
            d = float(r[f"demand_{z}"])
            if d > 0:
                row = np.zeros(nx + npen)
                for i in range(W):
                    row[xi(i, j, p)] = -1
                A.append(row); b.append(-d)
    # capacity (soft): sum x(i, ., p in k) - pen(i,k) <= remaining capacity
    for i, w in enumerate(WAREHOUSES):
        for k, kname in enumerate(STORAGE_TYPES):
            row = np.zeros(nx + npen)
            for p, r in ok.iterrows():
                if r["storage"] == kname:
                    for j in range(Z):
                        row[xi(i, j, p)] = 1
            row[pi(i, k)] = -1
            A.append(row); b.append(rem_cap.loc[w, kname])
    # share limits
    d_lp = float(ok[dem_cols].values.sum())
    manual_by_w = manual.groupby("warehouse")["units"].sum() if len(manual) else pd.Series(dtype=float)
    base = d_lp + (manual["units"].sum() if settings.count_rule_stock_in_shares and len(manual) else 0.0)
    for i, w in enumerate(WAREHOUSES):
        m_i = manual_by_w.get(w, 0.0) if settings.count_rule_stock_in_shares else 0.0
        row = np.zeros(nx + npen)
        for j in range(Z):
            for p in range(P):
                row[xi(i, j, p)] = 1
        A.append(row.copy()); b.append(limits.loc[w, "max_pct"] / 100 * base - m_i)
        A.append(-row); b.append(-(limits.loc[w, "min_pct"] / 100 * base - m_i))

    say("Solving")
    res = linprog(c, A_ub=np.array(A), b_ub=np.array(b), bounds=bounds, method="highs",
                  options={"time_limit": float(settings.time_limit_s)})
    status_map = {0: "Optimal", 1: "Suboptimal", 2: "Infeasible", 3: "Unbounded", 4: "Numerical error"}
    status = status_map.get(res.status, "Error")
    solved = res.x is not None and status in ("Optimal", "Suboptimal")

    # ---- 5. combine --------------------------------------------------------------------
    say("Saving results")
    opt_rows, pen = [], np.zeros((W, K))
    if solved:
        x = res.x
        for p, r in ok.iterrows():
            for i, w in enumerate(WAREHOUSES):
                for j, z in enumerate(ZONES):
                    u = x[xi(i, j, p)]
                    if u > 1e-6:
                        cc = c[xi(i, j, p)]
                        opt_rows.append(dict(warehouse=w, zone=z, product_type=r["product_type"],
                                             storage=r["storage"], units=u, unit_cost=cc, cost=cc * u,
                                             method="Optimizer"))
        for i in range(W):
            for k in range(K):
                pen[i, k] = x[pi(i, k)]
    detail = pd.concat([manual, pd.DataFrame(opt_rows, columns=manual.columns)], ignore_index=True)
    detail["units"] = detail["units"].round(2)
    detail["cost"] = detail["cost"].round(2)
    detail["unit_cost"] = detail["unit_cost"].round(2)

    # summary by warehouse x storage
    alloc = detail.groupby(["warehouse", "storage"])["units"].sum()
    srows = []
    for i, w in enumerate(WAREHOUSES):
        for k, kname in enumerate(STORAGE_TYPES):
            a = float(alloc.get((w, kname), 0.0))
            cap = float(capacity.loc[w, kname])
            over = max(a - cap, 0.0)
            srows.append(dict(warehouse=w, storage=kname, allocated=round(a, 1), capacity=cap,
                              used_pct=round(100 * a / cap, 1) if cap else 0.0, overflow=round(over, 1)))
    summary = pd.DataFrame(srows)

    tot_units = float(detail["units"].sum())
    by_w = detail.groupby("warehouse")["units"].sum()
    shares = pd.DataFrame([
        dict(warehouse=w, units=round(float(by_w.get(w, 0.0)), 1),
             share_pct=round(100 * float(by_w.get(w, 0.0)) / tot_units, 1) if tot_units else 0.0,
             min_pct=limits.loc[w, "min_pct"], max_pct=limits.loc[w, "max_pct"])
        for w in WAREHOUSES])

    rule_units = float(detail.loc[detail["method"] != "Optimizer", "units"].sum())
    overflow_units = float(summary["overflow"].sum())
    kpis = dict(
        status=status,
        total_cost=round(float(detail["cost"].sum()), 2),
        units=round(tot_units, 1),
        optimizer_units=round(tot_units - rule_units, 1),
        rule_units=round(rule_units, 1),
        overflow_units=round(overflow_units, 1),
        penalty_cost=round(float(pen.sum() * settings.overflow_penalty), 2),
        solve_seconds=round(time.time() - t0, 2),
        message=str(res.message),
    )

    # ---- warnings ----------------------------------------------------------------------
    warns = []
    if not solved:
        warns.append(dict(severity="High", title=f"Solver result: {status}",
                          detail="No valid plan. Check that min/max shares fit the demand and the business rules."))
    for _, s in summary[summary["overflow"] > 0].iterrows():
        warns.append(dict(severity="High", title=f"{s['warehouse']} is over its {s['storage']} space",
                          detail=f"{s['overflow']:,.0f} units above capacity ({s['used_pct']:.0f}% used)."))
    if filled_lanes:
        how = "the warehouse average" if not settings.missing_cost_fill.startswith("Minimum") else "the minimum cost (cheapest lane!)"
        warns.append(dict(severity="High" if settings.missing_cost_fill.startswith("Minimum") else "Medium",
                          title=f"{len(filled_lanes)} shipping cost lane(s) were missing",
                          detail=f"Filled with {how}."))
    changed = [n for n in slow_notes if n["changed"]]
    if changed:
        warns.append(dict(severity="Medium", title=f"{len(changed)} slow seller(s) changed warehouse",
                          detail="Compared with the last approved run."))
    if settings.slow_tie_break == "Random":
        warns.append(dict(severity="Medium", title="Slow sellers were placed at random",
                          detail="Re-running can give a different plan. Use 'Lowest cost' or 'Keep last month's'."))
    n_rule = int((pd.Series(groups) != "Optimizer").sum())
    if n_rule:
        warns.append(dict(severity="Info", title=f"{n_rule} product(s) placed by business rules",
                          detail="Slow sellers, no-demand and restricted products skip the optimizer."))
    for _, s in shares.iterrows():
        if s["units"] and (s["share_pct"] < s["min_pct"] - 0.5 or s["share_pct"] > s["max_pct"] + 0.5):
            warns.append(dict(severity="Medium", title=f"{s['warehouse']} share is outside its target",
                              detail=f"{s['share_pct']:.1f}% vs target {s['min_pct']:.0f}–{s['max_pct']:.0f}%."))

    return dict(
        kpis=kpis,
        detail=detail,
        summary=summary,
        shares=shares,
        warnings=warns,
        filled_lanes=pd.DataFrame(filled_lanes),
        slow_notes=pd.DataFrame(slow_notes),
        slow_map=slow_map,
        groups=groups,
    )
