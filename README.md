# Board Flow Streamlit App

Streamlit app for A-share board fund-flow snapshots.

## Local Run

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Data Collection

The main collector uses `rt_k` first. If the returned stock count is incomplete, it retries. If retries still fail, it falls back to `rt_min_daily`.

```bash
python collect_snapshot.py --source rt_k
```

For historical intraday node backfill:

```bash
python backfill_intraday_nodes.py --date 20260525 --nodes 13:30 --output data/backfill_rt_min_daily_20260525_1330.csv
python backfill_intraday_nodes.py --date 20260525 --output data/backfill_rt_min_daily_20260525_1330.csv --merge
```

## Streamlit Cloud

Deploy `app.py` from this GitHub repo. Add `TUSHARE_TOKEN` as a GitHub Actions secret for scheduled collection.
