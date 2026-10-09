import numpy as np
import pytest
from scipy import stats

from plc_platform_backend.listening_studies.study_statistics import analyze_study


def fixture(participants=8, tracks=1, conditions=2, excerpts=3):
    generator = np.random.default_rng(21)
    definition = {"trials": []}
    responses = []
    for track in range(tracks):
        for excerpt in range(excerpts):
            trial_id = f"track-{track}-excerpt-{excerpt}-r0"
            definition["trials"].append({
                "id": trial_id, "repetition": 0,
                "excerpt": {"id": trial_id, "track_id": f"track-{track}"},
                "conditions": [{"id": f"plc-{condition}", "role": "plc"} for condition in range(conditions)],
            })
    participant_offsets = generator.normal(0, 4, participants)
    participant_condition_offsets = generator.normal(0, 3, (participants, conditions))
    for participant in range(participants):
        for trial in definition["trials"]:
            for condition in range(conditions):
                score = 45 + condition * 12 + participant_offsets[participant] + participant_condition_offsets[participant, condition] + generator.normal(0, 2)
                responses.append({"session_id": f"p-{participant}", "trial_id": trial["id"], "condition_id": f"plc-{condition}", "score": float(score), "answered": True})
    return definition, responses


def test_one_factor_anova_matches_independent_paired_t_squared():
    definition, responses = fixture()
    result = analyze_study(definition, responses)
    assert result["inference"]["available"]
    assert result["inference"]["method"] == "repeated_measures_anova"
    by_participant = {f"p-{index}": {"plc-0": [], "plc-1": []} for index in range(8)}
    for row in responses:
        by_participant[row["session_id"]][row["condition_id"]].append(row["score"])
    means = [[np.mean(values[condition]) for values in by_participant.values()] for condition in ("plc-0", "plc-1")]
    paired = stats.ttest_rel(*means)
    effect = result["inference"]["effects"][0]
    assert effect["F"] == pytest.approx(float(paired.statistic) ** 2)
    assert effect["p_uncorrected"] == pytest.approx(float(paired.pvalue))
    assert effect["df1"] == 1 and effect["df2"] == 7
    assert result["inference"]["pairwise"][0]["effect"] < -5
    assert result["conditions"][0]["ci95"]
    assert result == analyze_study(definition, responses)


def test_two_factor_balanced_anova_matches_statsmodels():
    import pandas as pd
    from statsmodels.stats.anova import AnovaRM

    definition, responses = fixture(tracks=2)
    result = analyze_study(definition, responses)
    trials = {trial["id"]: trial for trial in definition["trials"]}
    frame = pd.DataFrame([{**row, "track_id": trials[row["trial_id"]]["excerpt"]["track_id"]} for row in responses])
    cells = frame.groupby(["session_id", "track_id", "condition_id"], as_index=False).score.mean()
    reference = AnovaRM(cells, "score", "session_id", within=["condition_id", "track_id"]).fit().anova_table
    effects = result["inference"]["effects"]
    assert len(effects) == 3
    assert effects[0]["F"] == pytest.approx(reference.loc["condition_id", "F Value"])
    assert effects[1]["F"] == pytest.approx(reference.loc["track_id", "F Value"])
    assert effects[2]["F"] == pytest.approx(reference.loc["condition_id:track_id", "F Value"])


def test_holm_correction_uses_the_whole_displayed_pair_family():
    definition, responses = fixture(conditions=3)
    result = analyze_study(definition, responses)
    pairs = result["inference"]["pairwise"]
    ordered = sorted(pairs, key=lambda pair: pair["p"])
    previous = 0
    for index, pair in enumerate(ordered):
        previous = min(1, max(previous, (len(pairs) - index) * pair["p"]))
        assert pair["p_adjusted"] == pytest.approx(previous)


def test_descriptive_track_weighting_is_not_raw_response_weighting():
    definition = {"trials": [{"id": f"t-{index}", "repetition": 0, "excerpt": {"id": f"e-{index}", "track_id": "a" if index == 0 else "b"}, "conditions": [{"id": "plc", "role": "plc"}]} for index in range(4)]}
    responses = [{"session_id": participant, "trial_id": trial["id"], "condition_id": "plc", "score": 10 if index == 0 else 90} for participant in ("p1", "p2") for index, trial in enumerate(definition["trials"])]
    result = analyze_study(definition, responses)
    assert result["conditions"][0]["mean"] == 50
    assert result["participant_count"] == 2 and result["response_count"] == 8
    assert not result["inference"]["available"]


def test_single_participant_empty_and_constant_scores_do_not_invent_inference():
    definition, responses = fixture(participants=1)
    result = analyze_study(definition, responses)
    assert all(condition["ci95"] is None for condition in result["conditions"])
    assert not result["inference"]["available"]
    empty = analyze_study(definition, [])
    assert all(condition["mean"] is None for condition in empty["conditions"])
    definition, responses = fixture()
    for row in responses:
        row["score"] = 0
    result = analyze_study(definition, responses)
    assert result["conditions"][0]["mean"] == 0
    assert result["inference"]["reason"] == "constant_scores"


def test_incomplete_cells_use_guarded_mixed_effects_not_independent_anova():
    definition, responses = fixture(participants=8, tracks=2)
    responses = [row for row in responses if not (row["session_id"] == "p-0" and row["trial_id"].startswith("track-1") and row["condition_id"] == "plc-1")]
    result = analyze_study(definition, responses)
    assert result["missing_cells"] == 1
    assert result["inference"]["available"]
    assert result["inference"]["method"] == "mixed_effects_ml"
    assert result["inference"]["condition_test"]["df"] == 2
    assert result["inference"]["condition_test"]["test"] == "joint condition and condition-by-track effects"


def test_invalid_filters_are_rejected_and_ad_hoc_subsets_are_exploratory():
    definition, responses = fixture(tracks=2)
    with pytest.raises(ValueError):
        analyze_study(definition, responses, condition_ids=["reference"])
    with pytest.raises(ValueError):
        analyze_study(definition, responses, track_ids=["unknown"])
    with pytest.raises(ValueError):
        analyze_study(definition, responses, repetitions=[1])
    assert analyze_study(definition, responses, track_ids=["track-0"])["exploratory"]


def test_failed_mixed_fits_keep_descriptive_results_and_explicit_diagnostics(monkeypatch):
    import statsmodels.formula.api as smf
    definition, responses = fixture(tracks=2)
    responses = [row for row in responses if not (row["session_id"] == "p-0" and row["trial_id"].startswith("track-1") and row["condition_id"] == "plc-1")]
    attempts = []
    def singular(*args, **kwargs):
        attempts.append(kwargs["vc_formula"])
        raise np.linalg.LinAlgError("singular fixture")
    monkeypatch.setattr(smf, "mixedlm", singular)
    result = analyze_study(definition, responses)
    assert not result["inference"]["available"]
    assert result["conditions"][0]["mean"] is not None
    assert "participant_condition" in attempts[0]
    assert "participant_condition" not in attempts[1]
    assert "singular fixture" in result["inference"]["diagnostics"]


def test_constant_cohorts_do_not_report_spurious_zero_width_confidence():
    definition, responses = fixture()
    for row in responses:
        row["score"] = 0
    result = analyze_study(definition, responses)
    for condition in result["conditions"]:
        assert condition["ci95"] is None
        assert condition["ci_unavailable_reason"] == "no_between_participant_variation"
        assert sum(condition["score_histogram"]["counts"]) == condition["response_count"]
    import json
    json.dumps(result, allow_nan=False)
