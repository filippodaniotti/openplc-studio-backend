"""Participant-aware MUSHRA estimates; never treat clips as independent people."""
from itertools import combinations
from importlib.metadata import version
import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats

ANALYSIS_VERSION = "mushra-v4.1-lmm-tukey-qc-summaries"
ANALYSIS_ENVIRONMENT = {name: version(name) for name in ("numpy", "pandas", "scipy", "statsmodels")}
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 1


def _unavailable(reason: str):
    return {"available": False, "reason": reason}


def _estimated_marginal_means(designs, coefficients, covariance):
    """Pointwise Wald intervals, not Tukey intervals or bootstrap summaries."""
    means = []
    critical = float(stats.norm.ppf(0.975))
    for condition, design in designs.items():
        mean = float(design @ coefficients)
        variance = float(design @ covariance @ design)
        if not math.isfinite(mean) or not math.isfinite(variance) or variance <= 0:
            raise ValueError("Mixed-effects marginal mean covariance is not estimable")
        se = math.sqrt(variance)
        means.append({"id": condition, "mean": mean, "se": se,
                      "ci95": [mean - critical * se, mean + critical * se]})
    return means


def _gls_fixed_covariance(model, fitted):
    """Asymptotic GLS information, conditional on fitted variance components.

    At a variance-component boundary the joint observed Hessian can be singular
    even though the marginal data covariance and fixed effects are estimable.
    Sparse solves avoid constructing a dense ratings-by-ratings inverse.
    """
    from scipy.sparse import eye
    from scipy.sparse.linalg import splu

    marginal = eye(model.exog.shape[0], format="csc") * fitted.scale
    for variance, matrices in zip(fitted.vcomp, model.exog_vc.mats):
        design = matrices[0]  # The crossed-effects model has one constant group.
        marginal = marginal + variance * (design @ design.T)
    try:
        precision_design = splu(marginal.tocsc()).solve(model.exog)
    except RuntimeError as error:
        raise ValueError("Mixed-effects marginal covariance is not estimable") from error
    information = model.exog.T @ precision_design
    covariance = np.linalg.inv(information)
    if not np.all(np.isfinite(covariance)) or np.linalg.cond(covariance) > 1e12 or np.any(np.linalg.eigvalsh(covariance) <= 0):
        raise ValueError("Mixed-effects GLS fixed covariance is not estimable")
    return covariance


def _mixed_inference(rows: pd.DataFrame):
    import statsmodels.formula.api as smf
    from statsmodels.regression.mixed_linear_model import MixedLMParams
    from patsy.build import build_design_matrices

    if rows.session_id.nunique() < 5:
        return _unavailable("mixed_effects_requires_at_least_five_participants")
    if rows.excerpt_id.nunique() < 3:
        return _unavailable("mixed_effects_requires_at_least_three_excerpts")
    # Keep individual trials/repetitions: context is shared by the conditions
    # rated in one participant's trial, while excerpts are crossed with people.
    data = rows.copy()
    trial_keys = pd.MultiIndex.from_arrays([data.session_id.to_numpy(), data.trial_id.to_numpy()])
    data["participant_trial"] = pd.factorize(trial_keys)[0].astype(str)
    fixed = "score ~ C(condition_id) * C(track_id)"
    reduced = "score ~ C(track_id)"
    # A constant group with variance components is the crossed-effects form:
    # excerpt effects are shared across people, not nested inside participants.
    base_components = {
        "participant": "0 + C(session_id)",
        "participant_trial": "0 + C(participant_trial)",
        "excerpt": "0 + C(excerpt_id)",
    }
    slope_components = {**base_components, "participant_condition": "0 + C(session_id):C(condition_id)"}
    diagnostics = []
    for components in (slope_components, base_components):
        try:
            with warnings.catch_warnings(record=True) as notices:
                warnings.simplefilter("always")
                model = smf.mixedlm(fixed, data, groups=np.ones(len(data)), re_formula="0", vc_formula=components, use_sparse=True)
                null_model = smf.mixedlm(reduced, data, groups=np.ones(len(data)), re_formula="0", vc_formula=components, use_sparse=True)
                if model.exog is None or null_model.exog is None:
                    return _unavailable("mixed_effects_fixed_design_is_not_estimable")
                if np.linalg.matrix_rank(model.exog) != model.exog.shape[1]:
                    return _unavailable("mixed_effects_fixed_design_is_not_estimable")
                fitted = model.fit(reml=False, method=["lbfgs", "powell"], maxiter=300, disp=False)
                # Start the nested model at the full model's variance estimates.
                # Default starts can terminate at a much worse boundary optimum,
                # spuriously inflating the condition likelihood-ratio statistic.
                null_start = MixedLMParams.from_components(
                    fe_params=np.linalg.lstsq(null_model.exog, data.score.to_numpy(), rcond=None)[0],
                    cov_re=np.zeros((0, 0)), vcomp=fitted.vcomp / fitted.scale,
                )
                null = null_model.fit(start_params=null_start, reml=False, method=["lbfgs", "powell"], maxiter=300, disp=False)
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
            # A singular joint Hessian can yield enormous intercept variance
            # while contrasts still look plausible. Use estimable GLS information
            # instead, documenting that variance components are treated as fitted.
            covariance_method = "Joint observed-information fixed-effect covariance"
            if np.linalg.cond(covariance) > 1e12:
                covariance = _gls_fixed_covariance(model, fitted)
                covariance_method = "Asymptotic GLS fixed-effect covariance conditional on fitted variance components"
                diagnostics.append("Joint observed-information covariance was ill-conditioned; using GLS fixed-effect covariance conditional on fitted variance components")
            conditions = sorted(data.condition_id.unique())
            tracks = sorted(data.track_id.unique())
            designs = {}
            for condition in conditions:
                design_data = pd.DataFrame({"condition_id": [condition] * len(tracks), "track_id": tracks})
                designs[condition] = np.asarray(build_design_matrices([model.data.design_info], design_data)[0]).mean(axis=0)
            marginal_means = _estimated_marginal_means(designs, fitted.fe_params.to_numpy(), covariance)
            pairs = []
            tukey_critical = float(stats.studentized_range.ppf(0.95, len(conditions), np.inf)) / math.sqrt(2)
            for first, second in combinations(conditions, 2):
                contrast = designs[first] - designs[second]
                effect = float(contrast @ fitted.fe_params.to_numpy())
                variance = float(contrast @ covariance @ contrast)
                if not math.isfinite(effect) or not math.isfinite(variance) or variance <= 0:
                    raise ValueError("Mixed-effects contrast variance is not positive")
                se = math.sqrt(variance)
                pairs.append({
                    "first": first, "second": second, "effect": effect, "se": se,
                    "ci95": [effect - tukey_critical * se, effect + tukey_critical * se],
                    "p": float(2 * stats.norm.sf(abs(effect / se))),
                    "p_adjusted": float(stats.studentized_range.sf(math.sqrt(2) * abs(effect / se), len(conditions), np.inf)),
                })
            degrees = model.exog.shape[1] - null_model.exog.shape[1]
            raw_likelihood_ratio = float(2 * (fitted.llf - null.llf))
            if degrees < 1 or raw_likelihood_ratio < -1e-5:
                raise ValueError("Nested mixed-model likelihoods or condition degrees are inconsistent")
            likelihood_ratio = max(0.0, raw_likelihood_ratio)
            return {
                "available": True, "method": "mixed_effects_ml", "formula": fixed,
                "random_structure": components, "converged": True,
                "variance_components": {name: float(value) for name, value in zip(model.exog_vc.names, fitted.vcomp)},
                "residual_variance": float(fitted.scale),
                "fallback": components is base_components,
                "condition_test": {"test": "joint condition and condition-by-track effects", "likelihood_ratio": likelihood_ratio, "df": degrees, "p": float(stats.chi2.sf(likelihood_ratio, degrees))},
                "participant_count": int(data.session_id.nunique()), "response_count": len(data),
                "excerpt_count": int(data.excerpt_id.nunique()),
                "participant_trial_count": int(data.participant_trial.nunique()),
                "estimated_marginal_means": marginal_means, "marginal_mean_weighting": "Equal selected tracks",
                "marginal_mean_ci_method": "Pointwise 95% asymptotic normal Wald intervals (not Tukey-adjusted)",
                "marginal_mean_confidence_level": 0.95,
                **({"fixed_covariance_method": covariance_method} if covariance_method.startswith("Asymptotic GLS") else {}),
                "pairwise": pairs, "pairwise_correction": "Tukey", "pairwise_ci_correction": "Tukey",
                "pairwise_method": "Estimated marginal mean contrasts, asymptotic studentized range (df = infinity)",
                "correction_note": "Tukey-adjusted p-values and simultaneous 95% intervals use the model contrast covariance and infinite degrees of freedom. This is an asymptotic approximation, not a finite-sample Satterthwaite/Kenward–Roger test; family-wise calibration is approximate for unequal/correlated contrast variances.",
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
        records.append({"session_id": response["session_id"], "trial_id": trial["id"], "condition_id": response["condition_id"], "track_id": trial["excerpt"]["track_id"], "excerpt_id": trial["excerpt"]["id"], "score": score})
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
    result["missing_cells"] = expected_cells - len(cells)
    if len(selected_conditions) < 2:
        result["inference"] = _unavailable("requires_two_plc_conditions")
    elif rows.condition_id.nunique() != len(selected_conditions) or rows.track_id.nunique() != len(selected_tracks):
        result["inference"] = _unavailable("some_selected_conditions_or_tracks_have_no_observations")
    elif rows.score.std() == 0:
        result["inference"] = _unavailable("constant_scores")
    else:
        result["inference"] = _mixed_inference(rows)
    return result


def analyze_quality_control_study(definition: dict, responses: list[dict], *, track_ids: list[str] | None = None, condition_ids: list[str] | None = None, repetitions: list[int] | None = None) -> dict:
    """Separate exploratory fit; never alter the primary PLC-only model/family.

    Reuse the exact fitting/contrast implementation with an independent local
    definition whose rated QC conditions are temporarily treated as fixed groups.
    The published definition and original role identities remain unchanged.
    """
    roles = {condition["id"]: condition["role"] for trial in definition["trials"] for condition in trial["conditions"]}
    plc_ids = sorted(condition for condition, role in roles.items() if role == "plc")
    selected_plc = plc_ids if condition_ids is None else sorted(set(condition_ids))
    if not set(selected_plc) <= set(plc_ids):
        raise ValueError("Analysis filters do not belong to the study")
    qc_ids = sorted(condition for condition, role in roles.items() if role in ("hidden_reference", "anchor"))
    selected = sorted(set(selected_plc + qc_ids))
    metadata = {
        "analysis_version": ANALYSIS_VERSION, "exploratory": True,
        "scope": "Separate QC-inclusive exploratory LMM; not the primary PLC-only analysis",
        "comparison_family": "All pairs among selected PLC conditions, hidden reference and anchor",
        "conditions": [{"id": condition, "role": roles[condition]} for condition in selected],
    }
    if not qc_ids:
        return {**metadata, "inference": _unavailable("no_quality_control_conditions")}
    expanded = {**definition, "trials": [
        {**trial, "conditions": [
            {**condition, "role": "plc"} if condition["role"] in ("hidden_reference", "anchor") else condition
            for condition in trial["conditions"]
        ]} for trial in definition["trials"]
    ]}
    result = analyze_study(expanded, responses, track_ids=track_ids, condition_ids=selected, repetitions=repetitions)
    return {
        **metadata, "participant_count": result["participant_count"], "response_count": result["response_count"],
        "summaries": result["conditions"], "inference": result["inference"],
    }
