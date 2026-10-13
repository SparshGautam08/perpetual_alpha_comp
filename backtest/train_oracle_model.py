"""Train the final ML Model 1 (oracle forecast, 15 s) on all recorded data
and save it for the live shadow runner / bot.

    .venv/bin/python -m backtest.train_oracle_model

Same features (bot/features.py), hyperparameters and target as the model
that passed its locked out-of-sample test (E23, E27). Saves
models/oracle_gbm_v1.joblib plus a JSON with what it was trained on.
"""
import json
from pathlib import Path

import joblib
import pandas as pd
import sklearn

from bot.features import FEATURES

from . import ml_forecast as M

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "models"


def main():
    df = M.build(end=pd.Timestamp("2100-01-01", tz="UTC"))
    y = f"y_{M.MAIN_H}"
    model = M.models()["gbm"][1]().fit(df[M.FEATURES], df[y])
    OUT.mkdir(exist_ok=True)
    joblib.dump(model, OUT / "oracle_gbm_v1.joblib")
    # Baseline (E20 rule) for a side-by-side live comparison in shadow mode.
    from sklearn.linear_model import LinearRegression
    joblib.dump(LinearRegression().fit(df[["prem_dm"]], df[y]), OUT / "oracle_baseline_v1.joblib")
    meta = {"model": "HistGradientBoostingRegressor (E23 settings)", "target": f"par oracle log change next {M.MAIN_H} s, bp",
            "features": FEATURES, "train_from": str(df.index.min()), "train_to": str(df.index.max()),
            "rows": len(df), "sklearn": sklearn.__version__,
            "in_sample_r2": float(model.score(df[M.FEATURES], df[y]))}
    (OUT / "oracle_gbm_v1.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
