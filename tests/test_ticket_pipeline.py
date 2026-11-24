import pandas as pd
import pytest

from src.ticket_pipeline import build_report, clean_tickets, validate_schema


def make(rows):
    cols = ["ticket_id", "priority", "category", "resolution_time_hours", "status", "rating"]
    return pd.DataFrame(rows, columns=cols)


@pytest.fixture
def raw():
    return make([
        ["T1", " low ", "network", "2.0", "open", "4"],
        ["T2", "HIGH", "Network", "4.0", "closed", None],
        ["T3", "High", "Billing", "abc", "closed", "5"],      # non-numeric
        ["T4", "High", "Billing", "-1", "closed", "5"],       # negative
        ["T5", "Urgent", "Billing", "1", "open", "3"],        # invalid priority
        ["T6", "Low", "", "1", "open", "3"],                  # blank category
        ["T7", "Low", "Billing", None, "open", "3"],          # missing time
        ["T1", " low ", "network", "2.0", "open", "4"],       # exact dup
        ["T8", "Low", "Billing", "6.0", "open", "9"],         # bad rating
    ])


def test_schema_missing_column():
    with pytest.raises(ValueError, match="Missing required columns"):
        validate_schema(pd.DataFrame({"priority": ["Low"]}))


def test_schema_empty_and_duplicate_columns():
    with pytest.raises(ValueError):
        validate_schema(pd.DataFrame(columns=["ticket_id", "priority", "category", "resolution_time_hours"]))
    df = pd.DataFrame([[1, 2]], columns=["a", "a"])
    with pytest.raises(ValueError, match="Duplicate"):
        validate_schema(df)


def test_raw_not_mutated(raw):
    before = raw.copy(deep=True)
    clean_tickets(raw)
    pd.testing.assert_frame_equal(raw, before)


def test_cleaning_counts(raw):
    clean, rejected, stats = clean_tickets(raw)
    assert stats["exact_duplicates_removed"] == 1
    assert sorted(clean["ticket_id"]) == ["T1", "T2", "T8"]
    assert set(rejected["ticket_id"]) == {"T3", "T4", "T5", "T6", "T7"}
    assert stats["input_rows"] == 9
    assert stats["clean_rows"] + stats["rejected_rows"] + stats["exact_duplicates_removed"] == 9


def test_normalisation(raw):
    clean, _, _ = clean_tickets(raw)
    t1 = clean[clean.ticket_id == "T1"].iloc[0]
    assert t1.priority == "Low" and t1.category == "Network"


def test_missing_not_imputed(raw):
    clean, rejected, _ = clean_tickets(raw)
    assert clean["resolution_time_hours"].notna().all()
    assert "missing_resolution_time" in rejected.loc[rejected.ticket_id == "T7", "reject_reason"].iloc[0]
    # missing rating is kept as NA, not filled
    assert pd.isna(clean.loc[clean.ticket_id == "T2", "rating"].iloc[0])


def test_invalid_priority_not_guessed(raw):
    _, rejected, _ = clean_tickets(raw)
    assert "invalid_priority" in rejected.loc[rejected.ticket_id == "T5", "reject_reason"].iloc[0]


def test_out_of_range_rating_nulled_row_kept(raw):
    clean, _, stats = clean_tickets(raw)
    assert pd.isna(clean.loc[clean.ticket_id == "T8", "rating"].iloc[0])
    assert stats["ratings_out_of_range_nulled"] == 1


def test_aggregations_known_values():
    clean, _, _ = clean_tickets(make([
        ["A", "High", "Net", "2", "open", "4"],
        ["B", "High", "Net", "4", "open", None],
        ["C", "Low", "Bill", "10", "closed", "2"],
    ]))
    rep = build_report(clean)
    pri = rep["priority"]
    assert list(pri.index) == ["Low", "High"]  # canonical order
    assert pri.loc["High", "tickets"] == 2
    assert pri.loc["High", "avg_resolution_hours"] == 3.0
    assert pri.loc["High", "avg_rating"] == 4.0      # NA ignored, not treated as 0
    assert pri.loc["High", "rated_tickets"] == 1
    assert rep["category"].loc["Bill", "avg_resolution_hours"] == 10.0
    assert rep["rating_distribution"]["tickets"].tolist() == [0, 1, 0, 1, 0]


def test_zero_resolution_time_is_valid():
    clean, rejected, _ = clean_tickets(make([["A", "Low", "X", "0", "open", "3"]]))
    assert len(clean) == 1 and rejected.empty


def test_all_rows_invalid_gives_empty_report():
    clean, rejected, stats = clean_tickets(make([["A", "Low", "X", "bad", "open", "3"]]))
    assert clean.empty and stats["data_quality_percentage"] == 0.0
    assert build_report(clean)["priority"].empty


def test_bad_date_nulled_row_kept():
    df = make([["A", "Low", "X", "1", "open", "3"]])
    df["created_at"] = ["2026-13-45"]
    clean, _, stats = clean_tickets(df)
    assert len(clean) == 1 and pd.isna(clean.loc[0, "created_at"])
    assert stats["unparseable_dates"] == 1
