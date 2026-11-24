"""Repeatable Pandas pipeline: validate schema -> clean -> report.

Cleaning rules (assumptions)
----------------------------
R1  Column names are trimmed, lower-cased, spaces -> underscores.
R2  Text fields are trimmed; priority/status are Title-cased, category too.
    Blank strings become missing (NA).
R3  Exact duplicate rows are dropped (first kept); duplicate ticket_id with
    different content is flagged but both rows are kept for review.
R4  Non-numeric / negative resolution_time_hours -> invalid, row rejected.
    Missing resolution time is NEVER imputed (unknown != 0 or mean).
R5  Missing priority or category -> row rejected (cannot be summarised).
R6  priority must be in VALID_PRIORITIES, otherwise rejected (no guessing,
    e.g. "Urgent" is not silently mapped to "Critical").
R7  rating must be an integer 1-5. Missing rating is KEPT as NA (a ticket
    can be valid without feedback); out-of-range rating is set to NA and
    counted, the row itself stays valid.
R8  Unparseable created_at -> NA (ISO strings kept), counted, row stays valid.
Raw input is never mutated; every step works on a copy.
"""

from __future__ import annotations

from datetime import date

import pandas as pd


def _iso_date(value):
    """Return a normalised YYYY-MM-DD string, or NA if not a valid date."""
    if pd.isna(value):
        return pd.NA
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        return pd.NA


REQUIRED_SCHEMA = {
    "ticket_id": "string",
    "priority": "string",
    "category": "string",
    "resolution_time_hours": "numeric",
}
OPTIONAL_COLUMNS = ["created_at", "status", "rating"]
VALID_PRIORITIES = ["Low", "Medium", "High", "Critical"]
RATING_RANGE = (1, 5)


# ---------- Task A: schema ----------
def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]
    return out


def validate_schema(df: pd.DataFrame) -> None:
    """Raise ValueError if required columns are missing/duplicated or df empty."""
    if df.columns.duplicated().any():
        raise ValueError("Duplicate column names after normalization.")
    missing = [c for c in REQUIRED_SCHEMA if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    if df.empty:
        raise ValueError("Input contains no rows.")


# ---------- Task B: cleaning ----------
def clean_tickets(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return (clean, rejected, stats). `raw` is left unchanged."""
    df = normalize_columns(raw)
    validate_schema(df)
    n_input = len(df)

    for col in ["ticket_id", "priority", "category", "status"]:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip().replace("", pd.NA)
    for col in ["priority", "category", "status"]:
        if col in df.columns:
            df[col] = df[col].str.title()

    # R3 duplicates
    dup_exact = df.duplicated(keep="first")
    n_dup_exact = int(dup_exact.sum())
    df = df.loc[~dup_exact].copy()
    conflicting_ids = int(
        df["ticket_id"].dropna().duplicated(keep=False).sum()
    )

    # R4 numeric conversion
    df["resolution_time_hours"] = pd.to_numeric(
        df["resolution_time_hours"], errors="coerce"
    )

    # optional columns
    rating_out_of_range = 0
    if "rating" in df.columns:
        r = pd.to_numeric(df["rating"], errors="coerce")
        bad = r.notna() & (
            (r < RATING_RANGE[0]) | (r > RATING_RANGE[1]) | (r % 1 != 0)
        )
        rating_out_of_range = int(bad.sum())
        df["rating"] = r.mask(bad).astype("Int64")
    bad_dates = 0
    if "created_at" in df.columns:
        # stdlib validation (avoids pd.to_datetime, which segfaults on this
        # Python 3.15 / pandas 2.3.3 install). Stored as ISO strings.
        parsed = df["created_at"].map(_iso_date)
        bad_dates = int((parsed.isna() & df["created_at"].notna()).sum())
        df["created_at"] = parsed

    # rejection reasons
    reasons = pd.DataFrame(index=df.index)
    reasons["missing_priority_or_category"] = df[["priority", "category"]].isna().any(axis=1)
    reasons["invalid_priority"] = df["priority"].notna() & ~df["priority"].isin(VALID_PRIORITIES)
    reasons["missing_resolution_time"] = df["resolution_time_hours"].isna()
    reasons["negative_resolution_time"] = df["resolution_time_hours"] < 0
    reject = reasons.any(axis=1)

    rejected = df.loc[reject].copy()
    rejected["reject_reason"] = [
        ";".join(reasons.columns[list(flags)])
        for flags in reasons.loc[reject].to_numpy()
    ]
    clean = df.loc[~reject].reset_index(drop=True)

    stats = {
        "input_rows": n_input,
        "exact_duplicates_removed": n_dup_exact,
        "rows_sharing_ticket_id_with_different_content": conflicting_ids,
        "rejected_rows": len(rejected),
        "clean_rows": len(clean),
        "rejection_reasons": {c: int(reasons[c].sum()) for c in reasons},
        "ratings_out_of_range_nulled": rating_out_of_range,
        "unparseable_dates": bad_dates,
        "missing_ratings_kept": int(clean["rating"].isna().sum()) if "rating" in clean else 0,
        "data_quality_percentage": round(len(clean) / n_input * 100, 2),
    }
    return clean, rejected, stats


# ---------- Task C: aggregation ----------
def _summary(clean: pd.DataFrame, by: str) -> pd.DataFrame:
    agg = {"tickets": ("ticket_id", "size"),
           "avg_resolution_hours": ("resolution_time_hours", "mean")}
    if "rating" in clean.columns:
        agg["avg_rating"] = ("rating", "mean")
        agg["rated_tickets"] = ("rating", "count")
    out = clean.groupby(by, observed=True).agg(**agg)
    out = out.round(2).sort_values("tickets", ascending=False, kind="stable")
    return out


def build_report(clean: pd.DataFrame) -> dict[str, pd.DataFrame]:
    report = {}
    pri = _summary(clean, "priority")
    report["priority"] = pri.reindex([p for p in VALID_PRIORITIES if p in pri.index])
    report["category"] = _summary(clean, "category")
    if "status" in clean.columns:
        report["status"] = _summary(clean.dropna(subset=["status"]), "status")
    if "rating" in clean.columns:
        dist = clean["rating"].value_counts().reindex(range(1, 6), fill_value=0)
        report["rating_distribution"] = dist.rename("tickets").to_frame()
        report["rating_distribution"].index.name = "rating"
    return report


# ---------- orchestration ----------
def run_pipeline(raw_path: str, out_dir: str) -> dict:
    from pathlib import Path
    raw = pd.read_csv(raw_path, dtype=str, keep_default_na=True)
    clean, rejected, stats = clean_tickets(raw)
    report = build_report(clean)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    clean.to_csv(out / "tickets_clean.csv", index=False)
    rejected.to_csv(out / "tickets_rejected.csv", index=False)
    for name, table in report.items():
        table.to_csv(out / f"summary_{name}.csv")
    return {"clean": clean, "rejected": rejected, "stats": stats, "report": report}


if __name__ == "__main__":
    import json
    import sys
    raw_path = sys.argv[1] if len(sys.argv) > 1 else "data/raw/tickets_raw.csv"
    res = run_pipeline(raw_path, "data/processed")
    print("== DATA QUALITY ==")
    print(json.dumps(res["stats"], indent=2))
    for name, table in res["report"].items():
        print(f"\n== {name.upper()} SUMMARY ==")
        print(table.to_string())
    print("\n== REJECTED ==")
    print(res["rejected"][["ticket_id", "reject_reason"]].to_string(index=False))
