"""Participant-aware MUSHRA estimates; never treat clips as independent people."""
from itertools import combinations
from importlib.metadata import version
import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.multitest import multipletests

ANALYSIS_VERSION = "mushra-v1"
ANALYSIS_ENVIRONMENT = {name: version(name) for name in ("numpy", "pandas", "scipy", "statsmodels", "pingouin")}
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 1


def number(value):
    return float(value) if value is not None and math.isfinite(float(value)) else None


def _unavailable(reason: str):
    return {"available": False, "reason": reason}


def _holm(pairs):
    if pairs:
        # Keep unestimable contrasts in the declared family conservatively,
        # without fabricating a reported p-value for them.
        corrected = multipletests([pair["p"] if pair.get("p") is not None else 1 for pair in pairs], method="holm")[1]
        for pair, adjusted in zip(pairs, corrected):
            if pair.get("p") is not None:
                pair["p_adjusted"] = number(adjusted)
    return pairs


def _paired_comparisons(estimates: pd.DataFrame):
    pairs = []
    for first, second in combinations(sorted(estimates.columns), 2):
        differences = (estimates[first] - estimates[second]).dropna().to_numpy()
        mean = float(np.mean(differences))
        standard_error = float(stats.sem(differences)) if len(differences) > 1 else 0
        pair = {"first": first, "second": second, "effect": mean, "participants": len(differences), "p": None, "ci95": None}
        if standard_error > 0:
            result = stats.ttest_1samp(differences, 0)
            margin = float(stats.t.ppf(0.975, len(differences) - 1)) * standard_error
            pair.update(p=number(result.pvalue), ci95=[mean - margin, mean + margin])
        else:
            pair["reason"] = "zero_variance_or_insufficient_paired_contrasts"
        pairs.append(pair)
    return _holm(pairs)


def _balanced_anova(cells: pd.DataFrame, participant_estimates: pd.DataFrame):
    import pingouin as pg

    tracks = cells.track_id.nunique()
    within = ["condition_id", "track_id"] if tracks > 1 else "condition_id"
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter("always")
        table = pg.rm_anova(data=cells, dv="score", within=within, subject="session_id", detailed=True, correction=True, effsize="np2")
    effects = []
    for _, row in table.iterrows():
        if row["Source"] == "Error":
            continue
        effect = {
            "effect": row["Source"], "F": number(row.get("F")),
            "df1": number(row.get("ddof1", row.get("DF"))),
            "df2": number(row.get("ddof2", table.loc[table.Source == "Error", "DF"].iloc[0] if "DF" in table and any(table.Source == "Error") else None)),
            "p_uncorrected": number(row.get("p-unc", row.get("p_unc"))),
            "p_greenhouse_geisser": number(row.get("p-GG-corr", row.get("p_GG_corr"))),
            "epsilon": number(row.get("eps")),
            "partial_eta_squared": number(row.get("np2")),
        }
        effects.append(effect)
    if not effects or any(effect["F"] is None or effect["F"] < 0 or effect["p_uncorrected"] is None for effect in effects):
        return _unavailable("anova_has_zero_residual_variance_or_nonfinite_statistics")
    return {
        "available": True, "method": "repeated_measures_anova",
        "formula": "score ~ condition" if tracks == 1 else "score ~ condition * track (both within participant)",
        "effects": effects, "pairwise": _paired_comparisons(participant_estimates),
        "pairwise_correction": "Holm", "pairwise_method": "paired participant-level t contrasts",
        "diagnostics": list(dict.fromkeys(str(notice.message) for notice in notices)),
        "correction_note": "Greenhouse–Geisser values are shown when supplied by pingouin; two-factor interaction epsilon is an approximation, not a formal interaction sphericity test.",
    }


def _mixed_inference(rows: pd.DataFrame):
    import statsmodels.formula.api as smf
    from patsy import build_design_matrices

    if rows.session_id.nunique() < 5:
        return _unavailable("mixed_effects_requires_at_least_five_participants")
    if rows.excerpt_id.nunique() < 3:
        return _unavailable("mixed_effects_requires_at_least_three_excerpts")
    data = rows.groupby(["session_id", "excerpt_id", "track_id", "condition_id"], as_index=False).score.mean()
    fixed = "score ~ C(condition_id) * C(track_id)"
    reduced = "score ~ C(track_id)"
    # A constant group with variance components is the crossed-effects form:
    # excerpt effects are shared across people, not nested inside participants.
    base_components = {"participant": "0 + C(session_id)", "excerpt": "0 + C(excerpt_id)"}
    slope_components = {**base_components, "participant_condition": "0 + C(session_id):C(condition_id)"}
    diagnostics = []
    for components in (slope_components, base_components):
        try:
            with warnings.catch_warnings(record=True) as notices:
                warnings.simplefilter("always")
                model = smf.mixedlm(fixed, data, groups=np.ones(len(data)), re_formula="0", vc_formula=components, use_sparse=True)
                null_model = smf.mixedlm(reduced, data, groups=np.ones(len(data)), re_formula="0", vc_formula=components, use_sparse=True)
                if np.linalg.matrix_rank(model.exog) != model.exog.shape[1]:
                    return _unavailable("mixed_effects_fixed_design_is_not_estimable")
                fitted = model.fit(reml=False, method=["lbfgs", "powell"], maxiter=300, disp=False)
                null = null_model.fit(reml=False, method=["lbfgs", "powell"], maxiter=300, disp=False)
            diagnostics.extend(str(notice.message) for notice in notices)
            if not fitted.converged or not null.converged or not math.isfinite(fitted.llf) or not math.isfinite(null.llf):
                raise ValueError("Mixed-effects fitting did not converge")
            if fitted.scale <= 1e-8 or null.scale <= 1e-8:
                raise ValueError("Mixed-effects residual variance is on a singular boundary")
            if components is slope_components:
                slope_index = model.exog_vc.names.index("participant_condition")
                if fitted.vcomp[slope_index] < 1e-8:
                    raise ValueError("Random-slope variance is on a singular boundary")
            coefficient_names = fitted.fe_params.index
            covariance = fitted.cov_params().loc[coefficient_names, coefficient_names].to_numpy()
            if not np.all(np.isfinite(covariance)) or np.any(np.linalg.eigvalsh(covariance) < -1e-8):
                raise ValueError("Mixed-effects fixed covariance is not estimable")
            conditions = sorted(data.condition_id.unique())
            tracks = sorted(data.track_id.unique())
            designs = {}
            for condition in conditions:
                design_data = pd.DataFrame({"condition_id": [condition] * len(tracks), "track_id": tracks})
                designs[condition] = np.asarray(build_design_matrices([model.data.design_info], design_data)[0]).mean(axis=0)
            pairs = []
            for first, second in combinations(conditions, 2):
                contrast = designs[first] - designs[second]
                effect = float(contrast @ fitted.fe_params.to_numpy())
                variance = float(contrast @ covariance @ contrast)
                if variance <= 0:
                    raise ValueError("Mixed-effects contrast variance is not positive")
                se = math.sqrt(variance)
                pairs.append({"first": first, "second": second, "effect": effect, "ci95": [effect - 1.96 * se, effect + 1.96 * se], "p": float(2 * stats.norm.sf(abs(effect / se)))})
            degrees = model.exog.shape[1] - null_model.exog.shape[1]
            raw_likelihood_ratio = float(2 * (fitted.llf - null.llf))
            if degrees < 1 or raw_likelihood_ratio < -1e-5:
                raise ValueError("Nested mixed-model likelihoods or condition degrees are inconsistent")
            likelihood_ratio = max(0.0, raw_likelihood_ratio)
            return {
                "available": True, "method": "mixed_effects_ml", "formula": fixed,
                "random_structure": components, "converged": True,
                "fallback": components is base_components,
                "condition_test": {"test": "joint condition and condition-by-track effects", "likelihood_ratio": likelihood_ratio, "df": degrees, "p": float(stats.chi2.sf(likelihood_ratio, degrees))},
                "pairwise": _holm(pairs), "pairwise_correction": "Holm", "pairwise_method": "fixed-effect Wald contrasts (normal approximation)",
                "diagnostics": list(dict.fromkeys(diagnostics)),
                "assumptions": "Participant-condition deviations have independent variance components; missingness must be plausibly ignorable. Mixed models do not repair non-random missingness.",
            }
        except (ValueError, np.linalg.LinAlgError, ZeroDivisionError, FloatingPointError) as error:
            diagnostics.append(str(error))
    return {**_unavailable("mixed_effects_unidentifiable_or_nonconvergent"), "diagnostics": list(dict.fromkeys(diagnostics))}


def analyze_study(definition: dict, responses: list[dict], *, track_ids: list[str] | None = None, condition_ids: list[str] | None = None, repetitions: list[int] | None = None) -> dict:
    trials = {trial["id"]: trial for trial in definition["trials"]}
    plc_conditions = sorted({condition["id"] for trial in trials.values() for condition in trial["conditions"] if condition["role"] == "plc"})
    selected_conditions = plc_conditions if condition_ids is None else sorted(set(condition_ids))
    available_tracks = {trial["excerpt"]["track_id"] for trial in trials.values()}
    selected_tracks = available_tracks if track_ids is None else set(track_ids)
    available_repetitions = {trial["repetition"] for trial in trials.values()}
    if not set(selected_conditions) <= set(plc_conditions) or not selected_tracks <= available_tracks or (repetitions is not None and not set(repetitions) <= available_repetitions):
        raise ValueError("Analysis filters do not belong to the study")
    records = []
    for response in responses:
        trial = trials.get(response["trial_id"])
        if trial is None or not response.get("answered", True) or response["condition_id"] not in selected_conditions:
            continue
        if trial["excerpt"]["track_id"] not in selected_tracks or (repetitions is not None and trial["repetition"] not in repetitions):
            continue
        score = response["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("Analysis received an invalid score")
        records.append({"session_id": response["session_id"], "condition_id": response["condition_id"], "track_id": trial["excerpt"]["track_id"], "excerpt_id": trial["excerpt"]["id"], "score": score})
    result = {
        "analysis_version": ANALYSIS_VERSION, "dependency_versions": ANALYSIS_ENVIRONMENT,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES, "confidence_level": 0.95,
        "weighting": "Equal participants, equal observed tracks within participant, averaged excerpts/repetitions within track",
        "ci_method": "Condition-wise marginal participant-cluster percentile bootstrap",
        "distribution_unit": "Participant track-balanced means; histogram shows unweighted individual ratings",
        "exploratory": any(value is not None for value in (track_ids, condition_ids, repetitions)),
        "participant_count": 0, "response_count": len(records), "conditions": [],
        "inference": _unavailable("no_included_responses"),
    }
    if not records:
        result["conditions"] = [{"id": condition, "mean": None, "median": None, "sd": None, "iqr": None, "ci95": None, "participant_count": 0, "response_count": 0} for condition in selected_conditions]
        return result
    rows = pd.DataFrame(records)
    cells = rows.groupby(["session_id", "track_id", "condition_id"], as_index=False).score.mean()
    estimates = cells.groupby(["session_id", "condition_id"]).score.mean().unstack("condition_id")
    result["participant_count"] = rows.session_id.nunique()
    generator = np.random.default_rng(BOOTSTRAP_SEED)
    for condition in selected_conditions:
        values = estimates[condition].dropna().to_numpy() if condition in estimates else np.array([])
        interval = None
        raw_values = rows.loc[rows.condition_id == condition, "score"].to_numpy()
        histogram, edges = np.histogram(raw_values, bins=np.linspace(0, 100, 11))
        if len(values) >= 2 and np.ptp(values) > 0:
            sampled = generator.choice(values, size=(BOOTSTRAP_RESAMPLES, len(values)), replace=True).mean(axis=1)
            interval = np.quantile(sampled, [0.025, 0.975]).tolist()
        result["conditions"].append({
            "id": condition, "participant_count": len(values), "response_count": int(sum(rows.condition_id == condition)),
            "mean": float(np.mean(values)) if len(values) else None,
            "median": float(np.median(values)) if len(values) else None,
            "sd": float(np.std(values, ddof=1)) if len(values) >= 2 else None,
            "iqr": float(np.quantile(values, 0.75) - np.quantile(values, 0.25)) if len(values) else None,
            "ci95": interval,
            "ci_unavailable_reason": None if interval else ("fewer_than_two_participants" if len(values) < 2 else "no_between_participant_variation"),
            "score_histogram": {"edges": edges.tolist(), "counts": histogram.tolist()},
        })
    expected_cells = result["participant_count"] * len(selected_tracks) * len(selected_conditions)
    balanced = len(cells) == expected_cells
    result["missing_cells"] = expected_cells - len(cells)
    if result["participant_count"] < 3 or len(selected_conditions) < 2:
        result["inference"] = _unavailable("requires_three_participants_and_two_plc_conditions")
    elif rows.condition_id.nunique() != len(selected_conditions) or rows.track_id.nunique() != len(selected_tracks):
        result["inference"] = _unavailable("some_selected_conditions_or_tracks_have_no_observations")
    elif rows.score.std() == 0:
        result["inference"] = _unavailable("constant_scores")
    elif balanced:
        try:
            result["inference"] = _balanced_anova(cells, estimates)
        except (ValueError, np.linalg.LinAlgError, ZeroDivisionError, FloatingPointError) as error:
            result["inference"] = {**_unavailable("repeated_measures_anova_not_estimable"), "diagnostics": [str(error)]}
    else:
        result["inference"] = _mixed_inference(rows)
    return result
