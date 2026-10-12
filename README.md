# Allocation Planner (Streamlit)

A working version of the Allocation Planner wireframes, with a login page and two roles.

| Role | Can do |
|---|---|
| **Planner** | Set up a monthly run (month, warehouse limits, space, demand, settings) → pre-run checks → **Run allocation** → review the plan and warnings → **Submit for approval** |
| **Approver** | See the approval queue → review the plan and warnings → **Approve** or **Send back** with a comment |

Both roles have **Plan**, **Warnings** and **Run history** pages.

## What the model does

`allocation/model.py` is a simplified, self-contained version of the production pipeline
(`model_main.py` → `optimization.py` → `alloc_solver_v2.py`):

1. Sorts products into groups: optimizer, slow seller, no demand, restricted
2. Places the special products by rules (slow seller → 1 warehouse, no demand → even split, restricted → its warehouse)
3. Subtracts that stock from warehouse space
4. Solves a linear program for the rest: **minimise shipping cost + overflow penalty**, subject to demand, min/max
   warehouse share, storage space (soft, with a penalty), forbidden warehouses and the medium-velocity building limit
5. Combines everything into one plan, with warnings

It uses **SciPy's HiGHS** solver instead of Gurobi, so it runs anywhere without a licence.
Several fixes from the code review are options in **Solver & rule settings**:

- slow-seller tie-break: *Lowest cost* / *Keep last month's* / *Random* (the legacy behaviour);
- missing costs filled with the warehouse average instead of the minimum cost;
- rule-based stock counted in the warehouse shares.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Demo accounts (used only while no users are set in secrets):

| Role | Username | Password |
|---|---|---|
| Planner | `maria` | `planner123` |
| Approver | `david` | `approver123` |

## Deploy and share on Streamlit Community Cloud

1. Create a **GitHub repository** (private is fine) and push this folder to it:
   ```bash
   git init && git add . && git commit -m "Allocation Planner app"
   git branch -M main
   git remote add origin https://github.com/<you>/allocation-planner.git
   git push -u origin main
   ```
2. Go to **share.streamlit.io**, sign in with GitHub, click **Create app** → *Deploy a public app from GitHub*.
3. Choose the repo, branch `main`, main file `app.py`. Pick a URL such as `allocation-planner-demo`.
4. Open **Advanced settings → Secrets** and paste your users (see below), then **Deploy**.
5. Share the app:
   - **Anyone with the link:** the login page still protects the app.
   - **Only specific people:** in the app's **Settings → Sharing**, make the app private and invite viewers by email.

### Adding real users

Passwords are stored only as salted hashes. Generate one block per user:

```bash
python -m allocation.auth priya "Priya Shah" planner "a-strong-password"
python -m allocation.auth raj   "Raj Mehta"  approver "another-strong-password"
```

Paste the printed blocks into `.streamlit/secrets.toml` (locally) or the **Secrets** box on Streamlit Cloud.
`secrets.toml.example` shows the format. As soon as any `[users.*]` block exists, the demo accounts are switched off.

## Limits of this version (read before using with real data)

- **Login is demo-grade.** For company use, switch to SSO, e.g. Streamlit's `st.login()` with Okta / Azure AD / Google.
  Keep the role check (planner / approver) the same.
- **Runs are stored in `data/runs.json`** on the app server. On Streamlit Cloud this file is wiped when the app restarts
  or redeploys. For real use, store runs in BigQuery, Cloud SQL or Firestore (swap `allocation/store.py`).
- **Sample data only.** Replace `sample_products()`, `sample_capacity()` and `BASE_COST` with reads from BigQuery /
  the `query_parameters.pkl` the pipeline already builds.
- **Don't put confidential Macy's data on a public Streamlit Cloud app.** Host it inside the company (Cloud Run / GKE)
  or use a private app with SSO.

## Files

```
app.py                     Streamlit UI: login, pages, approval workflow
allocation/model.py        Sample data + allocation model (rules + HiGHS LP)
allocation/store.py        Shared run store (JSON file)
allocation/auth.py         Login check, password hashing, user helper
.streamlit/config.toml     Theme
.streamlit/secrets.toml.example
requirements.txt
```
