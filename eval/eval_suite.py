"""
Evaluation suite for the natural-language-to-SQL layer (local LLM over DuckDB).

How it works
------------
Each test is (question, reference_sql). The reference SQL is hand-written and runs
against the RAW tables (trials, conditions, funders, phases, interventions), while the
model writes SQL against the cleaned view v_trials. Keeping the two independent means a
bug in the view shows up as a failure instead of being hidden.

Results are compared by value, not by column name:
  - column order inside a row is ignored, and row order is ignored
  - floats are rounded to 2 decimals
  - if the reference returns a single column, the model may return extra columns
    (for example a count beside the answer) as long as the values match
  - label text must still match, so "Female-only" vs "Female" counts as a miss in the
    strict score (see RECORDED_RESULTS for how that was reviewed)

Run settings used for the recorded results
------------------------------------------
model        qwen3.5:4b via Ollama (RTX 4050 laptop GPU, 6 GB)
temperature  0
num_ctx      6144
thinking     off, on every attempt
attempts     up to 3 per question, DuckDB error fed back between attempts
prompt       prompts/system_prompt_v1.txt (frozen before the held-out run)

Usage (from the notebook, with `con` and `ask` already defined)
---------------------------------------------------------------
    from eval_suite import EVAL, EVAL2, HELD_OUT, EXCLUDED, preflight, run_suite
    preflight(EVAL + EVAL2 + HELD_OUT, con)          # catches broken references first
    run_suite(EVAL + EVAL2, con, ask)                # regression suite (25 questions)
    run_suite(HELD_OUT, con, ask, excluded=EXCLUDED) # held-out set, scored on valid questions
"""
import math

# ---------------------------------------------------------------------------
# Regression suite, part 1 (written alongside the prompt)
# ---------------------------------------------------------------------------
EVAL = [
    ("How many trials are observational studies?",
     """SELECT COUNT(*) FROM trials WHERE "Study Type" = 'Observational'"""),
    ("How many trials started before 2019?",
     """SELECT COUNT(*) FROM trials WHERE "Start Date" < DATE '2019-01-01'"""),
    ("What is the median trial duration in days?",
     """SELECT MEDIAN(trial_duration_days) FROM trials"""),
    ("How many trials does each funder type back?",
     """SELECT funder, COUNT(DISTINCT nct_number) FROM funders GROUP BY funder"""),
    ("What are the top 5 intervention types by number of trials?",
     """SELECT intervention_type, COUNT(DISTINCT nct_number) FROM interventions
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5"""),
    ("What is the median enrollment of Phase 3 trials?",
     """SELECT MEDIAN("Enrollment") FROM trials
        WHERE "NCT Number" IN (SELECT nct_number FROM phases WHERE phase = 'Phase 3')"""),
    ("What is the median enrollment for each phase, excluding Not Applicable?",
     """SELECT p.phase, MEDIAN(t."Enrollment") FROM trials t
        JOIN phases p ON t."NCT Number" = p.nct_number
        WHERE p.phase != 'Not Applicable' GROUP BY p.phase"""),
    ("How many Industry-funded trials are in Phase 2?",
     """SELECT COUNT(DISTINCT f.nct_number) FROM funders f
        JOIN phases p ON f.nct_number = p.nct_number
        WHERE f.funder = 'Industry' AND p.phase = 'Phase 2'"""),
    ("How many trials study both covid-19 and depression?",
     """SELECT COUNT(DISTINCT a.nct_number) FROM conditions a
        JOIN conditions b ON a.nct_number = b.nct_number
        WHERE a.condition = 'covid-19' AND b.condition = 'depression'"""),
    ("Which condition other than covid-19 has the most trials?",
     """SELECT condition FROM conditions WHERE condition != 'covid-19'
        GROUP BY condition ORDER BY COUNT(DISTINCT nct_number) DESC LIMIT 1"""),
]

# ---------------------------------------------------------------------------
# Regression suite, part 2 (questions from earlier testing rounds, including
# ones that originally failed)
# ---------------------------------------------------------------------------
EVAL2 = [
    ("What is the median enrollment by condition for hypertension, diabetes, obesity and depression?",
     """SELECT c.condition, MEDIAN(t."Enrollment") FROM trials t
        JOIN conditions c ON t."NCT Number" = c.nct_number
        WHERE c.condition IN ('hypertension','diabetes','obesity','depression')
        GROUP BY c.condition"""),
    ("How many of the trials are suspended, terminated, or withdrawn?",
     """SELECT COUNT(*) FROM trials WHERE Status IN ('Suspended','Terminated','Withdrawn')"""),
    ("How many trials with condition covid-19 have the status Recruiting?",
     """SELECT COUNT(DISTINCT t."NCT Number") FROM trials t
        JOIN conditions c ON t."NCT Number" = c.nct_number
        WHERE c.condition = 'covid-19' AND t.Status = 'Recruiting'"""),
    ("How many of the trials have the placeholder completion date 2099-12-31?",
     """SELECT COUNT(*) FROM trials WHERE "Completion Date" = DATE '2099-12-31'"""),
    ("List the titles of the top 5 trials with the highest enrollment.",
     """SELECT Title FROM trials ORDER BY "Enrollment" DESC NULLS LAST LIMIT 5"""),
    ("How many trials are female-only, and how many are male-only?",
     """SELECT Gender, COUNT(*) FROM trials WHERE Gender IN ('Female','Male') GROUP BY Gender"""),
    ("What is the average minimum age and the average maximum age across trials?",
     """SELECT AVG(min_age), AVG(max_age) FROM trials"""),
    ("Excluding covid-19, which condition has the most recruiting trials?",
     """SELECT c.condition FROM trials t JOIN conditions c ON t."NCT Number" = c.nct_number
        WHERE t.Status = 'Recruiting' AND c.condition != 'covid-19'
        GROUP BY c.condition ORDER BY COUNT(DISTINCT t."NCT Number") DESC LIMIT 1"""),
    ("Among conditions other than covid-19 with at least 20 trials, which have the smallest and the largest median enrollment?",
     """WITH m AS (
          SELECT c.condition, MEDIAN(t."Enrollment") AS med
          FROM trials t JOIN conditions c ON t."NCT Number" = c.nct_number
          WHERE c.condition != 'covid-19'
          GROUP BY c.condition HAVING COUNT(DISTINCT t."NCT Number") >= 20)
        SELECT condition, med FROM m
        WHERE med = (SELECT MIN(med) FROM m) OR med = (SELECT MAX(med) FROM m)"""),
    ("Among completed and recruiting trials, report the count and percentage of trials for each gender category (All, Female, Male, Missing) within each status.",
     """SELECT Status, COALESCE(Gender,'Missing') AS gender, COUNT(*) AS trials,
               ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY Status), 2) AS pct
        FROM trials WHERE Status IN ('Completed','Recruiting')
        GROUP BY Status, COALESCE(Gender,'Missing')"""),
    ("How many trials are co-funded by Industry and NIH?",
     """SELECT COUNT(*) FROM (SELECT nct_number FROM funders WHERE funder IN ('Industry','NIH')
        GROUP BY nct_number HAVING COUNT(DISTINCT funder) = 2)"""),
    ("How many trials recruit participants under 18?",
     """SELECT COUNT(*) FROM trials WHERE population_group LIKE '%Child%'"""),
    ("How many trials recruit only female participants?",
     """SELECT COUNT(*) FROM trials WHERE Gender = 'Female'"""),
    ("How many trials exclusively enroll participants aged 65 and above?",
     """SELECT COUNT(*) FROM trials WHERE population_group = 'Older Adult'"""),
    ("How many trials have no recorded funder?",
     """SELECT COUNT(*) FROM trials t WHERE NOT EXISTS
        (SELECT 1 FROM funders f WHERE f.nct_number = t."NCT Number")"""),
]

# ---------------------------------------------------------------------------
# Held-out set: written AFTER the prompt was frozen. Reference queries for the
# 3rd, 5th and 9th questions were corrected after the first run (see notes).
# ---------------------------------------------------------------------------
HELD_OUT = [
    ("How many trials were posted in the first quarter of 2020 (between January 1, 2020 and March 31, 2020)?",
     """SELECT COUNT(DISTINCT "NCT Number") FROM trials
        WHERE "First Posted" BETWEEN DATE '2020-01-01' AND DATE '2020-03-31'"""),
    ("Which status category has the highest average trial duration?",
     """SELECT Status, AVG(trial_duration_days) FROM trials
        GROUP BY Status ORDER BY AVG(trial_duration_days) DESC LIMIT 1"""),
    # corrected after run 1: the label is 'Small (<100)', not 'Small'
    ("How many recruiting trials fall into the 'Small' enrollment bucket?",
     """SELECT COUNT(DISTINCT "NCT Number") FROM trials
        WHERE Status = 'Recruiting' AND enrollment_bucket = 'Small (<100)'"""),
    ("What is the most common population group for trials that are backed by U.S. Fed funding?",
     """SELECT t.population_group FROM trials t
        JOIN funders f ON t."NCT Number" = f.nct_number
        WHERE f.funder = 'U.S. Fed'
        GROUP BY t.population_group ORDER BY COUNT(DISTINCT t."NCT Number") DESC LIMIT 1"""),
    # corrected after run 1: the question asks for one percentage, not a breakdown
    ("What percentage of trials with a 'Terminated' status were testing an Interventional study type?",
     """SELECT ROUND(100.0 * SUM(CASE WHEN "Study Type" = 'Interventional' THEN 1 ELSE 0 END) / COUNT(*), 2)
        FROM trials WHERE Status = 'Terminated'"""),
    ("How many trials have an unknown or missing completion date where the status is still marked as Completed?",
     """SELECT COUNT(DISTINCT "NCT Number") FROM trials
        WHERE Status = 'Completed' AND ("Completion Date" IS NULL OR "Completion Date" = DATE '2099-12-31')"""),
    # excluded from the score: tie at the cut-off (see EXCLUDED)
    ("Excluding the condition covid-19, list the top 3 conditions that have the highest number of Completed trials.",
     """SELECT c.condition, COUNT(DISTINCT t."NCT Number") FROM trials t
        JOIN conditions c ON t."NCT Number" = c.nct_number
        WHERE t.Status = 'Completed' AND c.condition != 'covid-19'
        GROUP BY c.condition ORDER BY 2 DESC LIMIT 3"""),
    ("Among completed trials, which funder is associated with the highest number of unique trials?",
     """SELECT f.funder FROM trials t JOIN funders f ON t."NCT Number" = f.nct_number
        WHERE t.Status = 'Completed'
        GROUP BY f.funder ORDER BY COUNT(DISTINCT t."NCT Number") DESC LIMIT 1"""),
    # corrected after run 1: one number (by construction the answer is 100, because
    # only interventional trials have a phase)
    ("Among trials with Phase 3, what percentage were interventional studies?",
     """SELECT ROUND(100.0 * COUNT(DISTINCT CASE WHEN t."Study Type" = 'Interventional' THEN t."NCT Number" END)
              / COUNT(DISTINCT t."NCT Number"), 2)
        FROM trials t JOIN phases p ON t."NCT Number" = p.nct_number WHERE p.phase = 'Phase 3'"""),
]

# Questions removed from scoring, with the reason. Matching is on the question text.
EXCLUDED = {
    "Excluding the condition covid-19, list the top 3 conditions that have the highest number of Completed trials.":
        "Tie at the cut-off: depression and diabetes both have 20 completed trials, so there is no single right top 3.",
}

# Dropped from HELD_OUT entirely (kept here for transparency):
#   "What is the breakdown of study types (Interventional vs Observational) within the 'Mega'
#    enrollment bucket?"  Invalid question: there is no 'Mega' bucket (buckets are Small,
#    Medium, Large, Very Large). The model silently mapped it to 'Very Large (10000+)'.


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def _v(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "None"
    if isinstance(x, float):
        x = round(x, 2)
        if x.is_integer():
            x = int(x)
    return str(x)


def norm(df):
    """Order-insensitive, column-order-insensitive representation of a result."""
    return sorted(tuple(sorted(_v(v) for v in r)) for r in df.itertuples(index=False))


def matches(got, expected):
    if got is None:
        return False
    if norm(got) == norm(expected):
        return True
    if expected.shape[1] == 1:  # allow extra columns when the reference has one column
        exp = sorted(_v(v) for v in expected.iloc[:, 0])
        return any(sorted(_v(v) for v in got[c]) == exp for c in got.columns)
    return False


def preflight(suite, con):
    """Run every reference query and flag ones that look broken (empty or all-zero).
    A reference returning nothing or 0 is usually a typo in the test, not a model failure."""
    for q, ref in suite:
        r = con.execute(ref).df()
        broken = r.empty or (r.shape == (1, 1) and r.iloc[0, 0] in (0, None))
        print("CHECK" if broken else "ok   ", "|", q[:80], "|", r.shape)


def run_suite(suite, con, ask, excluded=None):
    excluded = excluded or {}
    passed = total = 0
    for q, ref_sql in suite:
        if q in excluded:
            print("SKIP |", q, "\n  reason:", excluded[q])
            continue
        expected = con.execute(ref_sql).df()
        got = ask(q, show_sql=False)
        ok = matches(got, expected)
        total += 1
        passed += ok
        print("PASS" if ok else "FAIL", "|", q)
        if not ok:
            print("  expected:", norm(expected)[:5])
            ask(q, show_sql=True)  # temperature 0, so this reproduces the failing query
    print(f"\n{passed} / {total}")
    return passed, total


# ---------------------------------------------------------------------------
# Recorded results
# ---------------------------------------------------------------------------
RECORDED_RESULTS = """
REGRESSION SUITE (EVAL + EVAL2, 25 questions; the prompt was tuned on these)
  Strict score: 22/25. After manual review: 23/25.
  Misses:
    1. "How many trials are female-only, and how many are male-only?"
       Counts correct (162 / 44) but the model labelled the rows 'Female-only' / 'Male-only'.
       Label difference only; counted as a pass after review.
    2. "...which have the smallest and the largest median enrollment?"
       Model returned every condition with 20+ trials sorted by median instead of
       extracting the two extremes. Real failure.
    3. "How many trials exclusively enroll participants aged 65 and above?"
       Model added unrequested min_age/max_age conditions and an operator-precedence bug
       (A AND B AND C OR D), pulling in unrelated trials. Real failure.

HELD-OUT SET (written after the prompt was frozen)
  Run 1, original references:                      5/10
  Run 2, after correcting 3 reference queries and
         turning thinking off on all attempts:     7/10 as scored by the script
  Audited: 8 valid questions, 6 correct.
    Excluded: the 'Mega' question (no such bucket; it scored PASS in run 2, but the reason
              was not investigated and the question is invalid either way), and the top-3
              conditions question (tie at 20 trials).
    Real failures:
      1. "Posted in the first quarter of 2020": model used start_date, not first_posted.
      2. "Phase 3 percentage": two binder errors, then a retry that ran but returned
         65000.0 (650 interventional / a window count of 1 * 100). Correct answer: 100.
         The retry loop turned a loud error into a silent wrong answer.

KNOWN LIMITATIONS OF THIS EVALUATION
  - Small samples: counts only, no percentages.
  - Three held-out references were corrected after seeing results, so the held-out set is not
    perfectly blind.
  - References were written by the same person who wrote the prompt.
  - One model, one prompt, temperature 0; results may differ for other settings.
"""
