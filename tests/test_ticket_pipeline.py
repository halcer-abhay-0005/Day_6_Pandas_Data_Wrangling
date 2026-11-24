import pandas as pd
import pytest

from src.ticket_pipeline import (
    TicketDataError,
    build_report,
    clean_tickets,
    load_ticket_csv,
    run_pipeline,
    validate_schema,
)


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


# ---------- loader / review-derived regressions ----------
HEADER = "ticket_id,priority,category,resolution_time_hours\n"


def write(tmp_path, text, name="t.csv"):
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.mark.parametrize("value", ["inf", "-inf", "Infinity"])
def test_non_finite_resolution_rejected(tmp_path, value):
    raw = load_ticket_csv(write(tmp_path, HEADER + f"T1,High,IT,{value}\nT2,Low,Billing,2\n"))
    clean, rejected, stats = clean_tickets(raw)
    assert clean["resolution_time_hours"].tolist() == [2.0]
    assert rejected["reject_reason"].iloc[0] == "non_finite_resolution_time"
    assert stats["rejection_reasons"]["non_finite_resolution_time"] == 1
    assert build_report(clean)["category"]["avg_resolution_hours"].notna().all()


def test_extra_fields_raise_not_shift(tmp_path):
    with pytest.raises(TicketDataError, match="row 2 has 5 fields, expected 4"):
        load_ticket_csv(write(tmp_path, HEADER + "T1,High,IT,3,EXTRA\nT2,Low,Billing,2,X\n"))


def test_short_row_raises(tmp_path):
    with pytest.raises(TicketDataError, match="row 2 has 3 fields"):
        load_ticket_csv(write(tmp_path, HEADER + "T1,High,IT\n"))


@pytest.mark.parametrize("header", [
    "ticket_id,priority,priority,category,resolution_time_hours",    # identical
    "ticket_id,priority,category,resolution_time_hours, Priority",   # case/space variant
    "ticket_id,priority,category,resolution_time_hours,PRIORITY",
    "ticket_id,priority,category,resolution_time_hours,priority ",   # trailing space
])
def test_duplicate_headers_rejected(tmp_path, header):
    with pytest.raises(TicketDataError, match="Duplicate column names"):
        load_ticket_csv(write(tmp_path, header + "\n"))


def test_missing_and_non_file_paths(tmp_path):
    with pytest.raises(TicketDataError, match="File not found"):
        load_ticket_csv(tmp_path / "nope.csv")
    with pytest.raises(TicketDataError, match="Not a file"):
        load_ticket_csv(tmp_path)


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_empty_file(tmp_path, text):
    with pytest.raises(TicketDataError, match="empty"):
        load_ticket_csv(write(tmp_path, text))


def test_header_only_no_rows(tmp_path):
    with pytest.raises(TicketDataError, match="no rows"):
        clean_tickets(load_ticket_csv(write(tmp_path, HEADER)))


def test_missing_required_column_from_file(tmp_path):
    raw = load_ticket_csv(write(tmp_path, "ticket_id,priority\nT1,High\n"))
    with pytest.raises(TicketDataError, match="Missing required columns: category, resolution_time_hours"):
        clean_tickets(raw)


def test_non_utf8_rejected(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_bytes(HEADER.encode() + b"T1,High,\xff\xfe,3\n")
    with pytest.raises(TicketDataError, match="UTF-8"):
        load_ticket_csv(p)


def test_acronym_category_not_corrupted(tmp_path):
    raw = load_ticket_csv(write(tmp_path, HEADER + "T1,High,IT,3\nT2,Low,IT,2\nT3,Low,it,1\n"))
    clean, _, _ = clean_tickets(raw)
    assert set(clean["category"]) == {"IT"}


def test_end_to_end_run_pipeline_writes_outputs(tmp_path):
    text = HEADER + "T1,High,IT,3\nT2,Low,Billing,bad\n"
    p = write(tmp_path, text)
    res = run_pipeline(p, tmp_path / "out")
    assert len(res["clean"]) == 1 and len(res["rejected"]) == 1
    assert (tmp_path / "out" / "tickets_clean.csv").exists()
    assert p.read_text() == text  # raw untouched
