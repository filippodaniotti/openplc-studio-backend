import numpy as np
import pytest
from scipy import stats

from plc_platform_backend.listening_studies.study_statistics import analyze_study, analyze_quality_control_study, _estimated_marginal_means, _gls_fixed_covariance


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


def test_emm_intervals_use_fixed_effect_covariance_not_pairwise_or_bootstrap_variance():
    designs = {"a": np.array([1.0, 0.0]), "b": np.array([1.0, 1.0]), "balanced": np.array([1.0, 0.5])}
    covariance = np.array([[4.0, -1.0], [-1.0, 9.0]])
    means = _estimated_marginal_means(designs, np.array([40.0, 10.0]), covariance)
    for entry, expected_mean, expected_variance in zip(means, [40, 50, 45], [4, 11, 5.25]):
        assert entry["mean"] == expected_mean
        assert entry["se"] == pytest.approx(np.sqrt(expected_variance))
        margin = stats.norm.ppf(0.975) * np.sqrt(expected_variance)
        assert entry["ci95"] == pytest.approx([expected_mean - margin, expected_mean + margin])


@pytest.mark.parametrize("covariance", [np.zeros((2, 2)), np.full((2, 2), np.nan), -np.eye(2)])
def test_unestimable_emm_covariance_is_guarded(covariance):
    with pytest.raises(ValueError, match="marginal mean covariance"):
        _estimated_marginal_means({"a": np.array([1, 0])}, np.array([40, 10]), covariance)


def test_gls_fixed_covariance_matches_independent_dense_marginal_information():
    from types import SimpleNamespace
    from scipy.sparse import csc_matrix, eye
    design = np.array([[1., 0.], [1., 0.], [1., 1.], [1., 1.]])
    model = SimpleNamespace(exog=design, exog_vc=SimpleNamespace(mats=[[csc_matrix(np.ones((4, 1)))], [eye(4, format="csc")]]))
    fitted = SimpleNamespace(scale=2., vcomp=[4., 3.])
    marginal = 5 * np.eye(4) + 4 * np.ones((4, 4))
    expected = np.linalg.inv(design.T @ np.linalg.inv(marginal) @ design)
    np.testing.assert_allclose(_gls_fixed_covariance(model, fitted), expected, rtol=1e-12)


def test_ill_conditioned_joint_covariance_uses_documented_estimable_gls_information(monkeypatch):
    from statsmodels.regression.mixed_linear_model import MixedLMResultsWrapper
    definition, responses = fixture()
    original = getattr(MixedLMResultsWrapper, "cov_params")
    def ill_conditioned(self, *args, **kwargs):
        covariance = original(self, *args, **kwargs).copy()
        covariance.iloc[0, 0] = 1e20
        return covariance
    monkeypatch.setattr(MixedLMResultsWrapper, "cov_params", ill_conditioned)
    inference = analyze_study(definition, responses)["inference"]
    assert inference["available"], inference
    assert "GLS" in inference["fixed_covariance_method"]
    assert any("ill-conditioned" in message for message in inference["diagnostics"])
    assert all(0 < mean["se"] < 100 for mean in inference["estimated_marginal_means"])


def test_balanced_data_uses_lmm_and_preserves_trial_structure():
    definition, responses = fixture()
    result = analyze_study(definition, responses)
    inference = result["inference"]
    assert inference["available"], inference
    assert inference["method"] == "mixed_effects_ml"
    assert inference["pairwise_correction"] == "Tukey"
    assert "Pointwise" in inference["marginal_mean_ci_method"]
    assert inference["marginal_mean_confidence_level"] == 0.95
    for mean in inference["estimated_marginal_means"]:
        assert mean["se"] > 0
        assert mean["ci95"][0] < mean["mean"] < mean["ci95"][1]
    assert inference["condition_test"]["df"] == 1
    assert inference["participant_count"] == 8
    assert inference["participant_trial_count"] == 24
    assert inference["excerpt_count"] == 3
    assert inference["response_count"] == len(responses)
    assert "participant_trial" in inference["random_structure"]
    assert "excerpt" in inference["random_structure"]
    assert inference["pairwise"][0]["effect"] < -5
    # In this balanced design, GLS marginal contrasts equal the arithmetic
    # mean of matched differences, independently of fitted variance estimates.
    first = np.mean([row["score"] for row in responses if row["condition_id"] == "plc-0"])
    second = np.mean([row["score"] for row in responses if row["condition_id"] == "plc-1"])
    assert inference["pairwise"][0]["effect"] == pytest.approx(first - second)
    assert "effects" not in inference
    assert result["conditions"][0]["ci95"]
    assert result == analyze_study(definition, responses)


def test_two_track_balanced_lmm_uses_equal_track_marginal_means():
    definition, responses = fixture(tracks=2)
    result = analyze_study(definition, responses)
    inference = result["inference"]
    assert inference["available"], inference
    assert inference["condition_test"]["df"] == 2
    assert inference["marginal_mean_weighting"] == "Equal selected tracks"
    means = {item["id"]: item["mean"] for item in inference["estimated_marginal_means"]}
    assert inference["pairwise"][0]["effect"] == pytest.approx(means["plc-0"] - means["plc-1"])


def test_tukey_correction_and_simultaneous_intervals_use_all_condition_pairs():
    definition, responses = fixture(conditions=5)
    for row in responses:
        row["score"] -= 10 + 11.2 * int(row["condition_id"].split("-")[1])
    result = analyze_study(definition, responses)
    assert result["inference"]["available"], result["inference"]
    pairs = result["inference"]["pairwise"]
    assert len(pairs) == 10
    critical = stats.studentized_range.ppf(0.95, 5, np.inf) / np.sqrt(2)
    for pair in pairs:
        q = np.sqrt(2) * abs(pair["effect"] / pair["se"])
        assert pair["p_adjusted"] == pytest.approx(stats.studentized_range.sf(q, 5, np.inf))
        assert pair["p_adjusted"] >= pair["p"] - 1e-12
        assert pair["ci95"] == pytest.approx([pair["effect"] - critical * pair["se"], pair["effect"] + critical * pair["se"]])


def test_two_condition_tukey_is_equivalent_to_two_sided_normal_contrast():
    definition, responses = fixture()
    pair = analyze_study(definition, responses)["inference"]["pairwise"][0]
    assert pair["p_adjusted"] == pytest.approx(pair["p"], abs=1e-12)


@pytest.mark.parametrize("participants,excerpts,reason", [
    (4, 3, "mixed_effects_requires_at_least_five_participants"),
    (8, 2, "mixed_effects_requires_at_least_three_excerpts"),
])
def test_lmm_sample_guards_preserve_descriptive_results(participants, excerpts, reason):
    definition, responses = fixture(participants=participants, excerpts=excerpts)
    result = analyze_study(definition, responses)
    assert result["inference"] == {"available": False, "reason": reason}
    assert result["conditions"][0]["mean"] is not None


def test_repetitions_are_distinct_contexts_with_shared_excerpts():
    definition, responses = fixture()
    definition["trials"] += [{**trial, "id": trial["id"] + "-repeat", "repetition": 1} for trial in definition["trials"]]
    generator = np.random.default_rng(9)
    responses += [{**row, "trial_id": row["trial_id"] + "-repeat", "score": row["score"] + generator.normal(0, 2)} for row in responses]
    inference = analyze_study(definition, responses)["inference"]
    assert inference["available"], inference
    assert inference["excerpt_count"] == 3
    assert inference["participant_trial_count"] == 48
    assert inference["response_count"] == 96


def test_qc_ratings_are_outside_the_primary_model_and_tukey_family():
    definition, responses = fixture()
    for trial in definition["trials"]:
        trial["conditions"] += [{"id": "reference", "role": "hidden_reference"}, {"id": "anchor", "role": "anchor"}]
    responses += [{**row, "condition_id": condition, "score": score} for row in responses if row["condition_id"] == "plc-0" for condition, score in (("reference", 100), ("anchor", 0))]
    result = analyze_study(definition, responses)
    assert result["response_count"] == 48
    assert len(result["inference"]["pairwise"]) == 1
    assert {item["id"] for item in result["inference"]["estimated_marginal_means"]} == {"plc-0", "plc-1"}


def qc_fixture(**kwargs):
    definition, responses = fixture(**kwargs)
    for trial in definition["trials"]:
        trial["conditions"] += [
            {"id": "actual-hidden-id", "role": "hidden_reference"},
            {"id": "actual-anchor-id", "role": "anchor"},
            {"id": "explicit-reference", "role": "reference"},
        ]
    responses += [
        {**row, "condition_id": condition, "score": score}
        for row in responses if row["condition_id"] == "plc-0"
        for condition, score in (("actual-hidden-id", 96 + (row["score"] - 45) * .05),
                                 ("actual-anchor-id", 10 + row["score"] * .1), ("explicit-reference", 100))
    ]
    return definition, responses


def test_qc_inclusive_fit_has_its_own_four_condition_tukey_family_and_preserves_primary():
    from copy import deepcopy
    definition, responses = qc_fixture()
    inputs = deepcopy((definition, responses))
    primary = analyze_study(definition, responses)
    quality = analyze_quality_control_study(definition, responses)
    assert quality["exploratory"] is True
    assert (definition, responses) == inputs
    assert analyze_study(definition, responses) == primary
    assert primary["response_count"] == 48
    assert len(primary["inference"]["pairwise"]) == 1
    assert quality["response_count"] == 96
    summaries = {summary["id"]: summary for summary in quality["summaries"]}
    assert set(summaries) == {"actual-anchor-id", "actual-hidden-id", "plc-0", "plc-1"}
    assert summaries["actual-anchor-id"]["participant_count"] == 8
    assert summaries["actual-anchor-id"]["response_count"] == 24
    assert summaries["actual-anchor-id"]["mean"] == pytest.approx(np.mean([row["score"] for row in responses if row["condition_id"] == "actual-anchor-id"]))
    assert summaries["actual-anchor-id"]["ci95"] is not None
    assert quality["inference"]["available"], quality["inference"]
    assert {condition["id"]: condition["role"] for condition in quality["conditions"]} == {
        "actual-anchor-id": "anchor", "actual-hidden-id": "hidden_reference", "plc-0": "plc", "plc-1": "plc",
    }
    inference = quality["inference"]
    assert len(inference["estimated_marginal_means"]) == 4
    assert len(inference["pairwise"]) == 6
    critical = stats.studentized_range.ppf(.95, 4, np.inf) / np.sqrt(2)
    for pair in inference["pairwise"]:
        q = np.sqrt(2) * abs(pair["effect"] / pair["se"])
        assert pair["p_adjusted"] == pytest.approx(stats.studentized_range.sf(q, 4, np.inf))
        assert pair["ci95"] == pytest.approx([pair["effect"] - critical * pair["se"], pair["effect"] + critical * pair["se"]])


def test_qc_fit_keeps_original_ids_and_respects_plc_track_and_repetition_filters():
    definition, responses = qc_fixture(participants=4, tracks=2)
    quality = analyze_quality_control_study(definition, responses, condition_ids=["plc-1"], track_ids=["track-1"], repetitions=[0])
    assert {condition["id"] for condition in quality["conditions"]} == {"plc-1", "actual-anchor-id", "actual-hidden-id"}
    assert quality["response_count"] == 36
    assert quality["participant_count"] == 4
    assert quality["inference"]["reason"] == "mixed_effects_requires_at_least_five_participants"
    with pytest.raises(ValueError, match="filters"):
        analyze_quality_control_study(definition, responses, condition_ids=["actual-hidden-id"])


def test_qc_fit_preserves_metadata_when_ratings_or_controls_are_missing():
    definition, responses = fixture()
    assert analyze_quality_control_study(definition, responses)["inference"]["reason"] == "no_quality_control_conditions"
    definition, responses = qc_fixture()
    missing_qc = [row for row in responses if row["condition_id"].startswith("plc-")]
    quality = analyze_quality_control_study(definition, missing_qc)
    assert len(quality["conditions"]) == 4
    assert all(summary["mean"] is None and summary["response_count"] == 0 for summary in quality["summaries"] if summary["id"].startswith("actual-"))
    assert quality["inference"]["reason"] == "some_selected_conditions_or_tracks_have_no_observations"
    assert analyze_quality_control_study(definition, [])["inference"]["reason"] == "no_included_responses"


def test_singular_condition_deviations_retry_retaining_trial_and_excerpt_intercepts(monkeypatch):
    import statsmodels.formula.api as smf

    definition, responses = fixture()
    original = smf.mixedlm
    attempts = []
    def without_slopes(*args, **kwargs):
        attempts.append(kwargs["vc_formula"])
        if "participant_condition" in kwargs["vc_formula"]:
            raise np.linalg.LinAlgError("singular condition deviations")
        return original(*args, **kwargs)
    monkeypatch.setattr(smf, "mixedlm", without_slopes)
    inference = analyze_study(definition, responses)["inference"]
    assert inference["available"], inference
    assert inference["fallback"]
    assert set(attempts[-1]) == {"participant", "participant_trial", "excerpt"}
    assert "singular condition deviations" in inference["diagnostics"]


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


def test_incomplete_cells_use_the_same_guarded_mixed_effects_pipeline():
    definition, responses = fixture(participants=8, tracks=2)
    responses = [row for row in responses if not (row["session_id"] == "p-0" and row["trial_id"].startswith("track-1") and row["condition_id"] == "plc-1")]
    result = analyze_study(definition, responses)
    assert result["missing_cells"] == 1
    assert result["inference"]["available"]
    assert result["inference"]["method"] == "mixed_effects_ml"
    assert result["inference"]["condition_test"]["df"] == 2
    assert result["inference"]["condition_test"]["test"] == "joint condition and condition-by-track effects"


def test_null_effects_do_not_report_significant_contrasts_for_this_fixture():
    definition, responses = fixture()
    for row in responses:
        if row["condition_id"] == "plc-1":
            row["score"] -= 12
    inference = analyze_study(definition, responses)["inference"]
    assert inference["available"], inference
    assert inference["condition_test"]["p"] > 0.05
    pair = inference["pairwise"][0]
    assert pair["p_adjusted"] > 0.05
    assert pair["ci95"][0] < 0 < pair["ci95"][1]


def test_unestimable_track_condition_cells_keep_descriptives():
    definition, responses = fixture(tracks=2)
    responses = [row for row in responses if not (row["trial_id"].startswith("track-1") and row["condition_id"] == "plc-1")]
    result = analyze_study(definition, responses)
    assert result["inference"]["reason"] == "mixed_effects_fixed_design_is_not_estimable"
    assert result["conditions"][0]["mean"] is not None


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
