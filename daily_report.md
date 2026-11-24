# Day 06 Daily Report: Pandas Data Wrangling

Developer: Abhay Gour
Assignment: Day 06 - Pandas_Data_Wranglig
Date: 2026-11-23
Repository URL: https://github.com/halcer-abhay-0005/Day_6_Pandas_Data_Wrangling.git
Latest commit hash: 8a503e4
Pull Request: N/A — no pull request created

- **Topics learned:** 
    - Series and DataFrames
    - Cleaning data
    - Aggregation and joins
    - Data-quality checks .
- **What I can explain without notes:**
  - **Imputing vs retaining missing values:** Impute only when there is a justified, documented reason, such as a known default or a model-based estimate that won't bias results. Otherwise retain the value as missing. Here a missing resolution time is not 0 or the average, so the row is rejected. A missing rating is kept as NA, and averages skip it.
  - **`loc` vs `iloc`:** `loc` selects by label (index and column names, with inclusive slices), and `iloc` selects by integer position (exclusive slices). After filtering or dropping rows the labels no longer match positions, so mixing them up gives wrong rows.
  - **Why joins duplicate rows:** A key that appears more than once on either side produces every matching pair (a many-to-many join). The result has more rows than expected, and summed or averaged values get inflated. Check key uniqueness first, or use `validate=` in `merge`.
  - **String-to-number conversion risks:** `to_numeric(errors="coerce")` silently turns bad values like "abc" into NaN, which merges "invalid" with "missing". Without coerce it raises an error. Other risks are stray whitespace, commas or currency symbols, and an int column turning into float once NaN appears. Count the coerced values and report them, as the pipeline does.
  - **Pipeline design:** Validate the schema first, work on copies so raw data stays unchanged, send rejected rows to a separate file with a reason, and test the aggregations against known values.
- **Implementation completed:**
  - `src/ticket_pipeline.py`:
    - schema validation
    - documented cleaning rules
    - a rejected-rows file with reasons
    - priority, category, status and rating summaries
  - Raw data is left unchanged. Outputs go to `data/processed/`.
  - A messy sample dataset is in `data/raw/tickets_raw.csv`.
  - The README covers setup, assumptions and limitations.
- **Tests/experiment results:** 29 of 29 pytest tests pass (`evidence/test_results.txt`). The sample run turned 13 raw rows into 7 clean rows, 5 rejected rows and 1 removed duplicate. Data quality was 53.85% (`evidence/pipeline_output.txt`).
- **One bug or challenge and its root cause:**
  - **Challenge:** The pipeline crashed with exit code 139 (a segfault). Cause: `pd.to_datetime` and Timestamp creation segfault on this machine (Python 3.15 with pandas 2.3.3).
  - **Fix:** I validate `created_at` with `datetime.date.fromisoformat` and keep it as ISO strings.
  - **Other bug:** The first test run failed because `DataFrame.apply(axis=1)` on an empty frame returns a DataFrame, which broke assigning `reject_reason`. I replaced it with a list comprehension.
- **Review-derived fixes:** Checked against earlier code reviews (Day 3/4) and fixed:
  - infinite resolution times were accepted as valid
  - duplicate raw headers were renamed by pandas and accepted
  - extra or short rows silently shifted data
  - empty or missing files raised raw pandas/OS errors
  - `.title()` corrupted the category "IT" to "It"

  All now have regression tests.
- **One design decision and why:** Rows with missing or invalid resolution time, or an invalid priority, are rejected and logged. They are not imputed or mapped. A missing value is not the same as 0 or the average, and "Urgent" is not necessarily "Critical". Missing ratings are kept as missing, because a ticket can be valid without feedback, and averages ignore them.
- **Blocker and what I tried:** The Timestamp segfault above. I tried an explicit date format and checked the traceback with `faulthandler`. I then worked around it with the standard library. Nothing else is blocking.
- **Tomorrow's plan:** After Successfully passing Day-6 Task, will get Day-7 Task.
