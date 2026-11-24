"""Repeatable Pandas pipeline: validate schema -> clean -> report.

Cleaning rules (assumptions)
----------------------------
R1  Column names are trimmed, lower-cased, spaces -> underscores.
R2  Text fields are trimmed; priority/status are Title-cased; category case
    variants are unified to the most common spelling (keeps "IT" intact).
    Blank strings become missing (NA).
R3  Exact duplicate rows are dropped (first kept); duplicate ticket_id with
    different content is flagged but both rows are kept for review.
R4  Non-numeric / negative / infinite resolution_time_hours -> invalid, row rejected.
    Missing resolution time is NEVER imputed (unknown != 0 or mean).
R5  Missing priority or category -> row rejected (cannot be summarised).
R6  priority must be in VALID_PRIORITIES, otherwise rejected (no guessing,
    e.g. "Urgent" is not silently mapped to "Critical").
R7  rating must be an integer 1-5. Missing (blank) rating is KEPT as NA (a
    ticket can be valid without feedback); a non-blank unparseable rating
    ("abc") and an out-of-range/non-integer/infinite rating are each set to NA
    and counted separately, the row itself stays valid.
R5b Missing/blank ticket_id -> row rejected (missing_ticket_id).
R8  Unparseable created_at -> NA (ISO strings kept), counted, row stays valid.
Input shape (strict, in load_ticket_csv): duplicate headers (raw or after
normalization), rows with the wrong field count, empty/missing/undecodable
files all raise TicketDataError - nothing is guessed or shifted.
Raw input is never mutated; every step works on a copy.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
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
class TicketDataError(ValueError):
    """Raised for unreadable, malformed or ambiguous ticket input."""


def _norm_name(name: str) -> str:
    return str(name).strip().lower().replace(" ", "_")


def load_ticket_csv(path: str | Path) -> pd.DataFrame:
    """Read a CSV as strings with strict shape checks. Raw file is not modified.

    Uses the csv module (not pd.read_csv) so that duplicate raw headers are
    seen before pandas renames them (priority -> priority.1), rows with too
    many/few fields are rejected instead of shifting columns, and no column
    is silently turned into the index.
    """
    path = Path(path)
    if not path.exists():
        raise TicketDataError(f"File not found: {path}")
    if not path.is_file():
        raise TicketDataError(f"Not a file: {path}")
    try:
        # Decode bytes ourselves: read_text() would translate newlines and
        # splitlines() below would drop quoted line breaks.
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TicketDataError("CSV must be UTF-8 encoded.") from exc
    except OSError as exc:
        raise TicketDataError(f"Cannot read {path}: {exc.strerror}") from exc

    try:
        rows = [r for r in csv.reader(io.StringIO(text, newline=""), strict=True) if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise TicketDataError(f"Malformed CSV: {exc}") from exc
    if not rows:
        raise TicketDataError("CSV file is empty.")

    header, body = rows[0], rows[1:]
    raw_dupes = {h for h in header if header.count(h) > 1}
    if raw_dupes:
        raise TicketDataError(f"Duplicate column names: {', '.join(sorted(raw_dupes))}")
    normed = [_norm_name(h) for h in header]
    norm_dupes = {h for h in normed if normed.count(h) > 1}
    if norm_dupes:
        raise TicketDataError(
            f"Duplicate column names after normalization: {', '.join(sorted(norm_dupes))}"
        )
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise TicketDataError(
                f"Malformed CSV: row {line_no} has {len(row)} fields, expected {len(header)}."
            )
    df = pd.DataFrame(body, columns=header, dtype="string")
    return df.replace("", pd.NA)


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]
    return out


def validate_schema(df: pd.DataFrame) -> None:
    """Raise TicketDataError (a ValueError) if required columns are missing/duplicated or df empty."""
    if df.columns.duplicated().any():
        raise TicketDataError("Duplicate column names after normalization.")
    missing = [c for c in REQUIRED_SCHEMA if c not in df.columns]
    if missing:
        raise TicketDataError(f"Missing required columns: {', '.join(missing)}")
    if df.empty:
        raise TicketDataError("Input contains no rows.")


# ---------- Task B: cleaning ----------
def _canonical_case(series: pd.Series) -> pd.Series:
    """Map case-variants ("network", "Network") to one deterministic spelling."""
    valid = series.dropna()
    if valid.empty:
        return series
    counts = valid.value_counts()
    canon = {}
    for value in sorted(counts.index, key=lambda v: (-counts[v], v)):
        canon.setdefault(value.casefold(), value)
    return series.map(lambda v: canon[v.casefold()] if pd.notna(v) else v).astype("string")


def clean_tickets(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return (clean, rejected, stats). `raw` is left unchanged."""
    df = normalize_columns(raw)
    validate_schema(df)
    n_input = len(df)

    for col in ["ticket_id", "priority", "category", "status"]:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip().replace("", pd.NA)
    for col in ["priority", "status"]:
        if col in df.columns:
            df[col] = df[col].str.title()

    # R3 duplicates
    dup_exact = df.duplicated(keep="first")
    n_dup_exact = int(dup_exact.sum())
    df = df.loc[~dup_exact].copy()
    # Category: unify case variants to the most common spelling (ties ->
    # alphabetical) instead of .title(), which would corrupt "IT" -> "It".
    df["category"] = _canonical_case(df["category"])
    conflicting_ids = int(
        df["ticket_id"].dropna().duplicated(keep=False).sum()
    )

    # R4 numeric conversion
    df["resolution_time_hours"] = pd.to_numeric(
        df["resolution_time_hours"], errors="coerce"
    )
    # R4b: +inf/-inf are not real durations; count separately, then treat as invalid
    non_finite = np.isinf(df["resolution_time_hours"].astype("float64"))
    df.loc[non_finite, "resolution_time_hours"] = np.nan

    # optional columns
    rating_out_of_range = 0
    rating_invalid_text = 0
    if "rating" in df.columns:
        r = pd.to_numeric(df["rating"], errors="coerce")
        # non-blank source value that did not parse -> invalid, not "missing"
        invalid_text = r.isna() & df["rating"].astype("string").str.strip().replace("", pd.NA).notna()
        rating_invalid_text = int(invalid_text.sum())
        bad = r.notna() & (
            ~np.isfinite(r) | (r < RATING_RANGE[0]) | (r > RATING_RANGE[1]) | (r % 1 != 0)
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
    reasons["missing_ticket_id"] = df["ticket_id"].isna()
    reasons["missing_priority_or_category"] = df[["priority", "category"]].isna().any(axis=1)
    reasons["invalid_priority"] = df["priority"].notna() & ~df["priority"].isin(VALID_PRIORITIES)
    reasons["non_finite_resolution_time"] = non_finite
    reasons["missing_resolution_time"] = df["resolution_time_hours"].isna() & ~non_finite
    reasons["negative_resolution_time"] = df["resolution_time_hours"] < 0
    reasons = reasons.fillna(False).astype(bool)
    reject = reasons.any(axis=1)

    rejected = df.loc[reject].copy()
    rejected["reject_reason"] = [
        ";".join(reasons.columns[np.asarray(flags, dtype=bool)])
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
        "ratings_invalid_text_nulled": rating_invalid_text,
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
def _write_csv_atomic(df: pd.DataFrame, target: Path, index: bool) -> None:
    """Write to a temp file in the same directory, then atomically replace."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            df.to_csv(fh, index=index)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run_pipeline(raw_path: str | Path, out_dir: str | Path) -> dict:
    raw_file = Path(raw_path).resolve()  # resolves symlinks
    raw = load_ticket_csv(raw_file)
    clean, rejected, stats = clean_tickets(raw)
    report = build_report(clean)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    outputs = [(out / "tickets_clean.csv", clean, False),
               (out / "tickets_rejected.csv", rejected, False)]
    outputs += [(out / f"summary_{name}.csv", table, True) for name, table in report.items()]
    # Raw input must never be overwritten: check every target before writing any.
    for target, _, _ in outputs:
        if target.resolve() == raw_file or (target.exists() and os.path.samefile(target, raw_file)):
            raise TicketDataError(
                f"Output file {target} would overwrite the raw input {raw_file}; "
                "choose a different input path or output directory."
            )
    for target, df, index in outputs:
        _write_csv_atomic(df, target, index)
    return {"clean": clean, "rejected": rejected, "stats": stats, "report": report}


if __name__ == "__main__":
    import json
    import sys
    raw_path = sys.argv[1] if len(sys.argv) > 1 else "data/raw/tickets_raw.csv"
    try:
        res = run_pipeline(raw_path, "data/processed")
    except TicketDataError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print("== DATA QUALITY ==")
    print(json.dumps(res["stats"], indent=2))
    for name, table in res["report"].items():
        print(f"\n== {name.upper()} SUMMARY ==")
        print(table.to_string())
    print("\n== REJECTED ==")
    print(res["rejected"][["ticket_id", "reject_reason"]].to_string(index=False))
