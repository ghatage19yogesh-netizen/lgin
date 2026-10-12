"""
Allocation Planner — Streamlit app

Planner:  log in → set up & run allocation → review plan & warnings → submit for approval
Approver: log in → approval queue → review plan & warnings → approve / reject with comment

Run locally:   streamlit run app.py
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

import pandas as pd
import streamlit as st

from allocation import store
from allocation.auth import DEMO_PASSWORDS, check_login, load_users
from allocation.model import (STORAGE_TYPES, Settings, run_allocation, sample_capacity,
                              sample_limits, sample_products, validate)

st.set_page_config(page_title="Allocation Planner", page_icon=":material/warehouse:", layout="wide")

ss = st.session_state
PERSIST_KEYS = ["month", "penalty", "time_limit", "tie_break", "cost_fill", "count_rule"]
for _k in PERSIST_KEYS:  # keep widget values when switching pages
    if _k in ss:
        ss[_k] = ss[_k]

STATUS_BADGE = {
    "Draft": ":gray-badge[Draft]",
    "Submitted": ":blue-badge[Submitted for approval]",
    "Approved": ":green-badge[Approved]",
    "Rejected": ":red-badge[Rejected]",
}
NAV = {
    "planner": ["Set up & run", "Plan", "Warnings", "Run history"],
    "approver": ["Approvals", "Plan", "Warnings", "Run history"],
}
ICONS = {"Set up & run": "▶", "Plan": "▦", "Warnings": "!", "Run history": "≡", "Approvals": "✓"}


def goto(page: str, run_id: str | None = None):
    ss["_goto"] = page
    if run_id:
        ss["selected_run"] = run_id
    st.rerun()


def month_options() -> list[str]:
    today = date.today()
    out = []
    for i in range(0, 6):
        m = (today.month - 1 + i) % 12 + 1
        y = today.year + (today.month - 1 + i) // 12
        out.append(date(y, m, 1).strftime("%B %Y"))
    return out


# ======================================================================================
# Login
# ======================================================================================
def login_page():
    users, demo = load_users(st.secrets if _has_secrets() else {})
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.write("")
        st.write("")
        st.markdown("## Allocation Planner")
        st.caption("Monthly COM inventory allocation · sign in to continue")
        with st.form("login", border=True):
            username = st.text_input("Username", placeholder="e.g. maria")
            password = st.text_input("Password", type="password")
            ok = st.form_submit_button("Sign in", type="primary", width="stretch")
        if ok:
            user = check_login(users, username, password)
            if user:
                ss["user"] = user
                ss["_goto"] = NAV[user["role"]][0]
                st.rerun()
            else:
                st.error("Username or password is incorrect.")
        st.markdown("**Two roles**")
        c1, c2 = st.columns(2)
        with c1, st.container(border=True):
            st.markdown("**Planner**")
            st.caption("Sets up the run, reviews the plan and warnings, submits it for approval.")
        with c2, st.container(border=True):
            st.markdown("**Approver**")
            st.caption("Reviews submitted plans and approves or sends them back with a comment.")
        if demo:
            with st.expander("Demo accounts"):
                st.markdown(
                    f"| Role | Username | Password |\n|---|---|---|\n"
                    f"| Planner | `maria` | `{DEMO_PASSWORDS['maria']}` |\n"
                    f"| Approver | `david` | `{DEMO_PASSWORDS['david']}` |"
                )
                st.caption("Replace these with real accounts in Streamlit secrets before sharing widely.")


def _has_secrets() -> bool:
    try:
        return len(st.secrets) > 0
    except Exception:
        return False


# ======================================================================================
# Shared pieces
# ======================================================================================
def visible_runs() -> list[dict]:
    runs = store.list_runs()
    if ss["user"]["role"] == "approver":
        runs = [r for r in runs if r["status"] != "Draft"]
    return runs


def run_label(r: dict) -> str:
    return f"{r['month']} · {r['id']} · {r['status']}"


def pick_run() -> dict | None:
    runs = visible_runs()
    if not runs:
        if ss["user"]["role"] == "planner":
            st.info("No runs yet. Start one from **Set up & run**.")
            if st.button("Go to Set up & run", type="primary"):
                goto("Set up & run")
        else:
            st.info("No plans have been submitted for approval yet.")
        return None
    ids = [r["id"] for r in runs]
    sel = ss.get("selected_run")
    idx = ids.index(sel) if sel in ids else 0
    chosen = st.selectbox("Run", ids, index=idx, format_func=lambda i: run_label(next(r for r in runs if r["id"] == i)),
                          label_visibility="collapsed")
    ss["selected_run"] = chosen
    return store.get_run(chosen)


def run_header(run: dict, title: str):
    st.markdown(f"## {title} · {run['month']}")
    st.markdown(f"{STATUS_BADGE.get(run['status'], run['status'])} &nbsp; "
                f"<span style='color:#5E5E5A'>{run['id']} · run by {run['created_by']} on {run['created_at']}</span>",
                unsafe_allow_html=True)


# ======================================================================================
# Planner: Set up & run
# ======================================================================================
def page_setup():
    st.markdown("## New allocation run")
    st.caption("Set the month and warehouse limits. Checks run before the solver starts.")

    ss.setdefault("limits_df", sample_limits())
    ss.setdefault("capacity_df", sample_capacity())
    ss.setdefault("products_df", sample_products())

    left, right = st.columns([2, 1], gap="large")
    with left:
        st.selectbox("Target month", month_options(), key="month")

        st.markdown("#### Warehouse share limits")
        st.caption("Min / max share of all units each warehouse should hold.")
        ss["limits_df"] = st.data_editor(
            ss["limits_df"], key="limits_editor", width="stretch",
            column_config={
                "min_pct": st.column_config.NumberColumn("Min %", min_value=0, max_value=100, step=1, format="%d%%"),
                "max_pct": st.column_config.NumberColumn("Max %", min_value=0, max_value=100, step=1, format="%d%%"),
            })

        st.markdown("#### Warehouse space (units)")
        ss["capacity_df"] = st.data_editor(
            ss["capacity_df"], key="capacity_editor", width="stretch",
            column_config={k: st.column_config.NumberColumn(k, min_value=0, step=50, format="%d")
                           for k in STORAGE_TYPES})

        with st.expander("Products & demand"):
            st.caption("Demand per zone for the target month. Rules: *must_be_in* / *forbidden_in*; "
                       "medium-velocity products can only use their allowed warehouses.")
            ss["products_df"] = st.data_editor(
                ss["products_df"], key="products_editor", width="stretch", hide_index=True,
                disabled=["product_type", "description", "storage", "velocity", "rule", "rule_cfcs",
                          "allowed_cfcs", "cost_factor"],
                column_config={
                    "product_type": "Product type", "description": "Description",
                    "storage": "Storage", "velocity": "Velocity",
                    "demand_East": st.column_config.NumberColumn("East", min_value=0, format="%d"),
                    "demand_Central": st.column_config.NumberColumn("Central", min_value=0, format="%d"),
                    "demand_West": st.column_config.NumberColumn("West", min_value=0, format="%d"),
                    "order_qty": st.column_config.NumberColumn("Order qty (no demand)", min_value=0, format="%d"),
                    "rule": "Rule", "rule_cfcs": "Rule warehouses", "allowed_cfcs": "Allowed (medium)",
                    "cost_factor": "Cost factor",
                })

        with st.expander("Solver & rule settings"):
            ss.setdefault("penalty", 50.0)
            ss.setdefault("time_limit", 60)
            ss.setdefault("count_rule", True)
            c1, c2 = st.columns(2)
            c1.number_input("Overflow penalty ($ per unit over capacity)", min_value=0.0, step=5.0, key="penalty")
            c2.number_input("Time limit (seconds)", min_value=5, step=5, key="time_limit")
            c1.selectbox("Slow-seller tie-break", ["Lowest cost", "Keep last month's", "Random"], key="tie_break",
                         help="'Random' is how the current script works; it can change the plan between runs.")
            c2.selectbox("Missing cost fill", ["Warehouse average", "Minimum cost (legacy)"], key="cost_fill",
                         help="The legacy option makes missing lanes the cheapest — not recommended.")
            st.checkbox("Count rule-based stock in warehouse shares", key="count_rule")

        if st.button("Reset to sample data"):
            for k in ["limits_df", "capacity_df", "products_df", "limits_editor", "capacity_editor", "products_editor"]:
                ss.pop(k, None)
            st.rerun()

    with right:
        checks = validate(ss["products_df"], ss["capacity_df"], ss["limits_df"])
        has_error = any(c["level"] == "error" for c in checks)
        with st.container(border=True):
            st.markdown("#### Pre-run checks")
            icon = {"ok": ":green[✓]", "warn": ":orange[!]", "info": ":blue[i]", "error": ":red[✕]"}
            for c in checks:
                st.markdown(f"{icon[c['level']]} &nbsp; {c['text']}")
        run_clicked = st.button("Run allocation", type="primary", width="stretch", disabled=has_error)
        st.caption("Blocked until every ✕ check passes." if has_error
                   else "Runs the rules, then the optimiser. Usually takes a few seconds.")

    if run_clicked:
        settings = Settings(
            overflow_penalty=float(ss.get("penalty", 50.0)),
            time_limit_s=float(ss.get("time_limit", 60)),
            slow_tie_break=ss.get("tie_break", "Lowest cost"),
            missing_cost_fill=ss.get("cost_fill", "Warehouse average"),
            count_rule_stock_in_shares=bool(ss.get("count_rule", True)),
        )
        with st.status("Running allocation…", expanded=True) as status:
            res = run_allocation(ss["products_df"], ss["capacity_df"], ss["limits_df"], settings,
                                 previous_slow_map=store.last_approved_slow_map(),
                                 progress=lambda m: status.write(m))
            k = res["kpis"]
            status.update(label=f"Finished — {k['status']} in {k['solve_seconds']}s",
                          state="complete" if k["status"] in ("Optimal", "Suboptimal") else "error")
        run_id = "RUN-" + datetime.now().strftime("%y%m%d-%H%M%S")
        store.save_run({
            "id": run_id,
            "month": ss.get("month", month_options()[0]),
            "created_by": ss["user"]["name"],
            "created_by_user": ss["user"]["username"],
            "created_at": store.now(),
            "status": "Draft",
            "settings": asdict(settings),
            "inputs": {
                "limits": ss["limits_df"].reset_index().to_dict(orient="records"),
                "capacity": ss["capacity_df"].reset_index().to_dict(orient="records"),
            },
            "results": store.serialise_results(res),
            "history": [{"at": store.now(), "status": "Draft", "by": ss["user"]["name"]}],
        })
        goto("Plan", run_id)


# ======================================================================================
# Plan
# ======================================================================================
def page_plan():
    run = pick_run()
    if not run:
        return
    res = store.deserialise_results(run["results"])
    k = res["kpis"]
    run_header(run, "Allocation plan")

    msg = f"Solver: **{k['status']}** · finished in {k['solve_seconds']}s"
    if k["status"] == "Optimal":
        st.success("Best plan found. " + msg, icon=":material/check_circle:")
    elif k["status"] == "Suboptimal":
        st.warning("Stopped at the time limit — plan is usable but not proven best. " + msg)
    else:
        st.error("No valid plan. " + msg + " — see Warnings.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total shipping cost", f"${k['total_cost']:,.0f}", border=True)
    c2.metric("Units placed", f"{k['units']:,.0f}", border=True)
    opt_pct = 100 * k["optimizer_units"] / k["units"] if k["units"] else 0
    c3.metric("Optimizer vs rules", f"{opt_pct:.0f}% / {100 - opt_pct:.0f}%", border=True)
    c4.metric("Overflow units", f"{k['overflow_units']:,.0f}",
              delta=f"${k['penalty_cost']:,.0f} penalty" if k["overflow_units"] else None,
              delta_color="inverse", border=True)

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("#### By warehouse and storage type")
        summ = res["summary"].copy()
        if len(summ):
            top = max(100.0, float(summ["used_pct"].max()))
            st.dataframe(summ, hide_index=True, width="stretch", column_config={
                "warehouse": "Warehouse", "storage": "Storage",
                "allocated": st.column_config.NumberColumn("Allocated", format="%d"),
                "capacity": st.column_config.NumberColumn("Space", format="%d"),
                "used_pct": st.column_config.ProgressColumn("Space used", min_value=0, max_value=top, format="%.0f%%"),
                "overflow": st.column_config.NumberColumn("Over by", format="%d"),
            })
        st.markdown("#### Warehouse share vs target")
        sh = res["shares"].copy()
        if len(sh):
            sh["target"] = sh.apply(lambda r: f"{r['min_pct']:.0f}–{r['max_pct']:.0f}%", axis=1)
            sh["in_range"] = sh.apply(lambda r: "✓" if r["min_pct"] - 0.5 <= r["share_pct"] <= r["max_pct"] + 0.5 else "✕",
                                      axis=1)
            st.dataframe(sh[["warehouse", "units", "share_pct", "target", "in_range"]], hide_index=True,
                         width="stretch", column_config={
                             "warehouse": "Warehouse", "units": st.column_config.NumberColumn("Units", format="%d"),
                             "share_pct": st.column_config.NumberColumn("Share", format="%.1f%%"),
                             "target": "Target", "in_range": "In range"})
    with right:
        st.markdown("#### Cost by warehouse ($)")
        det = res["detail"]
        if len(det):
            pv = det.pivot_table(index="warehouse", columns="method", values="cost", aggfunc="sum", fill_value=0)
            palette = {"Optimizer": "#1C1C1A", "Slow seller": "#C26A1B", "Restricted": "#2F5DA8", "No demand": "#A9A69C"}
            cols = [c for c in palette if c in pv.columns and pv[c].sum() > 0]
            st.bar_chart(pv[cols], horizontal=True, height=240, color=[palette[c] for c in cols])
        st.markdown("#### Units by storage type")
        if len(res["summary"]):
            st.bar_chart(res["summary"].pivot_table(index="warehouse", columns="storage", values="allocated"),
                         horizontal=True, height=240, stack=False, color=["#1C1C1A", "#C26A1B"])

    with st.expander("Full allocation detail"):
        det = res["detail"]
        st.dataframe(det, hide_index=True, width="stretch")
        st.download_button("Download CSV", det.to_csv(index=False).encode(), file_name=f"{run['id']}_allocation.csv",
                           mime="text/csv")

    workflow_actions(run)


def workflow_actions(run: dict):
    role = ss["user"]["role"]
    st.divider()
    st.markdown("#### Approval")
    if run.get("comment"):
        st.info(f"**{run.get('decided_by', 'Approver')}:** {run['comment']}")
    if role == "planner":
        if run["status"] in ("Draft", "Rejected"):
            note = st.text_input("Note for the approver (optional)", key=f"note_{run['id']}")
            if st.button("Submit for approval", type="primary"):
                store.update_run(run["id"], status="Submitted", submitted_at=store.now(),
                                 submit_note=note, by=ss["user"]["name"], comment=None)
                st.toast("Sent for approval")
                st.rerun()
        elif run["status"] == "Submitted":
            st.caption("Waiting for the approver.")
        else:
            st.caption(f"Approved by {run.get('decided_by')} on {run.get('decided_at')}.")
    else:
        if run.get("submit_note"):
            st.caption(f"Planner’s note: {run['submit_note']}")
        if run["status"] == "Submitted":
            comment = st.text_area("Comment", key=f"comment_{run['id']}",
                                   placeholder="Required when sending back")
            a, r, _ = st.columns([1, 1, 3])
            if a.button("Approve plan", type="primary", width="stretch"):
                store.update_run(run["id"], status="Approved", decided_by=ss["user"]["name"],
                                 decided_at=store.now(), comment=comment or None, by=ss["user"]["name"])
                st.toast("Plan approved")
                st.rerun()
            if r.button("Send back", width="stretch"):
                if not comment.strip():
                    st.error("Add a comment so the planner knows what to change.")
                else:
                    store.update_run(run["id"], status="Rejected", decided_by=ss["user"]["name"],
                                     decided_at=store.now(), comment=comment, by=ss["user"]["name"])
                    st.toast("Sent back to planner")
                    st.rerun()
        else:
            st.caption(f"Status: {run['status']}.")
    with st.expander("History"):
        hist = pd.DataFrame(run.get("history", []))
        st.dataframe(hist, hide_index=True, width="stretch")


# ======================================================================================
# Warnings
# ======================================================================================
def page_warnings():
    run = pick_run()
    if not run:
        return
    res = store.deserialise_results(run["results"])
    run_header(run, "Things to check")

    warns = res.get("warnings", [])
    order = {"High": 0, "Medium": 1, "Info": 2}
    if not warns:
        st.success("Nothing to check for this run.")
    for w in sorted(warns, key=lambda w: order.get(w["severity"], 3)):
        badge = {"High": ":red-badge[High]", "Medium": ":orange-badge[Medium]"}.get(w["severity"], ":blue-badge[Info]")
        with st.container(border=True):
            st.markdown(f"{badge} &nbsp; **{w['title']}**")
            st.caption(w["detail"])

    t1, t2, t3 = st.tabs(["Rule-based placements", "Slow sellers", "Filled cost lanes"])
    det = res["detail"]
    with t1:
        rb = det[det["method"] != "Optimizer"] if len(det) else det
        if len(rb):
            g = (rb.groupby(["product_type", "method", "warehouse"], as_index=False)["units"].sum()
                   .rename(columns={"product_type": "Product type", "method": "Reason", "warehouse": "Warehouse",
                                    "units": "Units"}))
            st.dataframe(g, hide_index=True, width="stretch")
        else:
            st.caption("No rule-based placements.")
    with t2:
        sn = res["slow_notes"]
        if len(sn):
            st.dataframe(sn.rename(columns={"product_type": "Product type", "warehouse": "Warehouse", "how": "Chosen by",
                                            "last_month": "Last approved", "changed": "Changed"}),
                         hide_index=True, width="stretch")
        else:
            st.caption("No slow sellers in this run.")
    with t3:
        fl = res["filled_lanes"]
        if len(fl):
            st.dataframe(fl, hide_index=True, width="stretch")
        else:
            st.caption("All cost lanes had data.")


# ======================================================================================
# Approver: queue
# ======================================================================================
def page_approvals():
    st.markdown("## Approvals")
    runs = store.list_runs()
    pending = [r for r in runs if r["status"] == "Submitted"]
    st.caption(f"{len(pending)} plan(s) waiting for you.")
    if not pending:
        st.info("Nothing to approve right now.")
    for r in pending:
        k = r["results"]["kpis"]
        with st.container(border=True):
            a, b, c, d, e = st.columns([2.2, 1.2, 1.2, 1.2, 1])
            a.markdown(f"**{r['month']}**  \n<span style='color:#5E5E5A'>{r['id']} · {r['created_by']} · "
                       f"submitted {r.get('submitted_at', '')}</span>", unsafe_allow_html=True)
            b.metric("Cost", f"${k['total_cost']:,.0f}")
            c.metric("Units", f"{k['units']:,.0f}")
            d.metric("Warnings", len(r["results"].get("warnings", [])))
            if e.button("Review", key=f"rev_{r['id']}", type="primary", width="stretch"):
                goto("Plan", r["id"])
    decided = [r for r in runs if r["status"] in ("Approved", "Rejected")][:10]
    if decided:
        st.markdown("#### Recently decided")
        st.dataframe(pd.DataFrame([{"Run": r["id"], "Month": r["month"], "Status": r["status"],
                                    "By": r.get("decided_by"), "On": r.get("decided_at"),
                                    "Comment": r.get("comment") or ""} for r in decided]),
                     hide_index=True, width="stretch")


# ======================================================================================
# Run history
# ======================================================================================
def page_history():
    st.markdown("## Run history")
    runs = visible_runs()
    if not runs:
        st.info("No runs yet.")
        return
    df = pd.DataFrame([{
        "Run": r["id"], "Month": r["month"], "Status": r["status"], "Run by": r["created_by"],
        "Created": r["created_at"], "Solver": r["results"]["kpis"]["status"],
        "Cost ($)": r["results"]["kpis"]["total_cost"], "Overflow": r["results"]["kpis"]["overflow_units"],
    } for r in runs])
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"Cost ($)": st.column_config.NumberColumn(format="$%d")})
    c1, c2 = st.columns([3, 1])
    pick = c1.selectbox("Open a run", [r["id"] for r in runs],
                        format_func=lambda i: run_label(next(r for r in runs if r["id"] == i)))
    c2.write("")
    if c2.button("Open plan", width="stretch"):
        goto("Plan", pick)


# ======================================================================================
# Main
# ======================================================================================
def main():
    if "user" not in ss:
        login_page()
        return
    user = ss["user"]
    pages = NAV[user["role"]]
    if "_goto" in ss:
        target = ss.pop("_goto")
        if target in pages:
            ss["page"] = target
    if ss.get("page") not in pages:
        ss["page"] = pages[0]

    with st.sidebar:
        st.markdown("### Allocation Planner")
        st.markdown(f"**{user['name']}**  \n{user['role'].title()}")
        st.radio("Navigate", pages, key="page", label_visibility="collapsed",
                 format_func=lambda p: f"{ICONS.get(p, '')}  {p}")
        if user["role"] == "approver":
            n = sum(1 for r in store.list_runs() if r["status"] == "Submitted")
            if n:
                st.caption(f"{n} plan(s) waiting for approval")
        st.divider()
        if st.button("Log out", width="stretch"):
            for k in list(ss.keys()):
                del ss[k]
            st.rerun()

    {"Set up & run": page_setup, "Plan": page_plan, "Warnings": page_warnings,
     "Run history": page_history, "Approvals": page_approvals}[ss["page"]]()


main()
