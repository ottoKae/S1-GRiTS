"""Positive-Unlabeled (PU) learning for S1-GRiTS Data Cubes.

Implements the classic Elkan & Noto (KDD 2008) framework, "Learning
Classifiers from Only Positive and Unlabeled Data" — the common labelling
situation in SAR change/land-cover mapping: reliable POSITIVE points exist
(field-verified deforestation, flood, crop parcels), but there is no
trustworthy negative set, only the vast unlabeled remainder.

Core result: under the *selected completely at random* (SCAR) assumption —
labeled positives are a random sample of all positives — a standard
probabilistic classifier ``g(x)`` trained to separate labeled-vs-unlabeled
satisfies ``g(x) = c * p(y=1 | x)``, where the label frequency
``c = p(s=1 | y=1)`` is a CONSTANT. So the true posterior is recovered by
estimating one scalar:

    p(y=1 | x) = g(x) / c ,   with  c ≈ mean of g over held-out positives
                                     (the paper's estimator e1).

Two methods are provided:

- ``method="calibrated"`` (default): fit ``g`` on labeled-vs-unlabeled with a
  positive hold-out, estimate ``c`` on the hold-out, divide at predict time.
- ``method="weighted"``: the paper's second approach — after estimating
  ``c``, refit the base estimator treating each unlabeled example as a
  positive with weight ``w(x) = p(y=1 | x, s=0)`` AND a negative with weight
  ``1 - w(x)``. Often better calibrated when the base estimator supports
  ``sample_weight``.

The estimator is scikit-learn compatible (``fit(X, s)`` /
``predict_proba`` / ``predict``) and wraps any base classifier exposing
``fit`` + ``predict_proba``; scikit-learn itself is an optional dependency
(``pip install s1grits[ml]``), imported lazily only for the default
LogisticRegression.

Cube integration: :func:`pu_training_set` turns a ``(time, band, y, x)``
DataArray (as returned by :func:`s1grits.ml_loader.load_timeseries`) plus a
2-D positive mask into a per-pixel feature matrix using temporal reducers,
and :func:`predict_proba_map` paints calibrated probabilities back onto the
(y, x) grid.

Usage::

    from s1grits.ml_loader import load_timeseries
    from s1grits.analysis.pu_learning import (
        PUClassifier, pu_training_set, predict_proba_map,
    )

    cube = load_timeseries(root, collection="s1grits-smonthly", tile="17MPV")
    X, s, meta = pu_training_set(cube, positive_mask, random_state=0)
    clf = PUClassifier(random_state=0).fit(X, s)
    print(clf.c_, clf.prior_)              # label frequency, class prior
    prob = predict_proba_map(clf, cube, meta)   # (y, x) float32, NaN outside
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

_EPS = 1e-12


def _default_estimator(random_state=None):
    """LogisticRegression via a lazy scikit-learn import (the `ml` extra)."""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:  # pragma: no cover - exercised via message test
        raise ImportError(
            "PUClassifier's default base estimator needs scikit-learn: "
            "pip install 's1grits[ml]' — or pass any base_estimator with "
            "fit(X, y) and predict_proba(X)."
        ) from exc
    return make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=1000, random_state=random_state),
    )


class PUClassifier:
    """Elkan–Noto positive-unlabeled classifier.

    Parameters
    ----------
    base_estimator : object, optional
        Any classifier with ``fit(X, y)`` and ``predict_proba(X)``. Default:
        StandardScaler + LogisticRegression (requires the ``ml`` extra). For
        ``method="weighted"`` it must also accept ``sample_weight`` in
        ``fit``.
    method : {"calibrated", "weighted"}
        "calibrated" divides the labeled-vs-unlabeled score by the estimated
        label frequency ``c``; "weighted" refits with the paper's per-example
        weighting of unlabeled data.
    hold_out_ratio : float
        Fraction of labeled positives held out to estimate ``c`` (e1
        estimator). Ignored when ``c`` is given.
    c : float, optional
        Known label frequency ``p(s=1 | y=1)``. When provided, no hold-out is
        taken and the given value is used verbatim.
    random_state : int, optional
        Seed for the hold-out split (and the default estimator).

    Attributes
    ----------
    c_ : float
        Estimated (or provided) label frequency ``p(s=1 | y=1)``.
    prior_ : float
        Estimated class prior ``p(y=1)`` = mean(g) / c over the training set.
    estimator_ : object
        The fitted underlying estimator.
    """

    def __init__(
        self,
        base_estimator: Any = None,
        *,
        method: str = "calibrated",
        hold_out_ratio: float = 0.2,
        c: float | None = None,
        random_state: int | None = None,
    ):
        if method not in ("calibrated", "weighted"):
            raise ValueError(
                f"method={method!r} invalid; expected 'calibrated' or 'weighted'"
            )
        if not (0.0 < hold_out_ratio < 1.0):
            raise ValueError("hold_out_ratio must be in (0, 1)")
        if c is not None and not (0.0 < c <= 1.0):
            raise ValueError("c (label frequency) must be in (0, 1]")
        self.base_estimator = base_estimator
        self.method = method
        self.hold_out_ratio = hold_out_ratio
        self.c = c
        self.random_state = random_state

    # -- internals -----------------------------------------------------------

    def _make_estimator(self):
        if self.base_estimator is not None:
            import copy
            return copy.deepcopy(self.base_estimator)
        return _default_estimator(self.random_state)

    @staticmethod
    def _pos_proba(est, X) -> np.ndarray:
        """Probability of the POSITIVE (label 1) class, robust to class order."""
        proba = est.predict_proba(X)
        classes = getattr(est, "classes_", None)
        if classes is None:  # pipeline: classes_ lives on the final step
            classes = est[-1].classes_
        idx = int(np.flatnonzero(np.asarray(classes) == 1)[0])
        return np.asarray(proba)[:, idx]

    # -- sklearn-style API ---------------------------------------------------

    def fit(self, X, s) -> "PUClassifier":
        """Fit from features ``X`` and PU labels ``s`` (1 = labeled positive,
        0 = unlabeled). Raises if either group is empty."""
        X = np.asarray(X, dtype=np.float64)
        s = np.asarray(s).astype(int).ravel()
        if X.ndim != 2 or len(X) != len(s):
            raise ValueError("X must be (n_samples, n_features) aligned with s")
        if not set(np.unique(s)) <= {0, 1}:
            raise ValueError("s must contain only 0 (unlabeled) and 1 (positive)")
        pos_idx = np.flatnonzero(s == 1)
        unl_idx = np.flatnonzero(s == 0)
        if len(pos_idx) == 0 or len(unl_idx) == 0:
            raise ValueError(
                f"PU fitting needs both labeled positives and unlabeled "
                f"examples (got {len(pos_idx)} positive, {len(unl_idx)} unlabeled)"
            )

        rng = np.random.default_rng(self.random_state)

        if self.c is not None:
            # Known label frequency: train g on everything, no hold-out.
            hold = np.empty(0, dtype=int)
            train_mask = np.ones(len(s), dtype=bool)
        else:
            n_hold = max(1, int(round(len(pos_idx) * self.hold_out_ratio)))
            if len(pos_idx) - n_hold < 1:
                raise ValueError(
                    f"Too few labeled positives ({len(pos_idx)}) for "
                    f"hold_out_ratio={self.hold_out_ratio}: nothing left to train on"
                )
            hold = rng.choice(pos_idx, size=n_hold, replace=False)
            train_mask = np.ones(len(s), dtype=bool)
            train_mask[hold] = False

        g_est = self._make_estimator()
        g_est.fit(X[train_mask], s[train_mask])

        if self.c is not None:
            self.c_ = float(self.c)
        else:
            # e1: the non-traditional classifier's mean score over held-out
            # positives estimates c = p(s=1 | y=1).
            self.c_ = float(np.clip(
                self._pos_proba(g_est, X[hold]).mean(), _EPS, 1.0
            ))

        g_all = self._pos_proba(g_est, X)
        self.prior_ = float(np.clip(g_all.mean() / self.c_, 0.0, 1.0))

        if self.method == "calibrated":
            self.estimator_ = g_est
        else:
            # Weighted refit (paper §3): labeled positives keep weight 1 as
            # y=1; every unlabeled example appears twice, as y=1 with weight
            # w(x) = p(y=1 | x, s=0) and as y=0 with weight 1 - w(x).
            g_unl = np.clip(g_all[unl_idx], _EPS, 1.0 - _EPS)
            w = ((1.0 - self.c_) / self.c_) * (g_unl / (1.0 - g_unl))
            w = np.clip(w, 0.0, 1.0)
            Xw = np.concatenate([X[pos_idx], X[unl_idx], X[unl_idx]])
            yw = np.concatenate([
                np.ones(len(pos_idx)), np.ones(len(unl_idx)),
                np.zeros(len(unl_idx)),
            ])
            sw = np.concatenate([np.ones(len(pos_idx)), w, 1.0 - w])
            est = self._make_estimator()
            if hasattr(est, "steps"):
                # sklearn Pipelines route fit params as <final_step>__<param>
                fit_params = {f"{est.steps[-1][0]}__sample_weight": sw}
            else:
                fit_params = {"sample_weight": sw}
            try:
                est.fit(Xw, yw.astype(int), **fit_params)
            except TypeError as exc:
                raise TypeError(
                    "method='weighted' requires a base estimator whose fit() "
                    "accepts sample_weight"
                ) from exc
            self.estimator_ = est

        logger.info(
            "[PU] fitted method=%s on %d positives / %d unlabeled: "
            "c=%.4f (label frequency), prior p(y=1)=%.4f",
            self.method, len(pos_idx), len(unl_idx), self.c_, self.prior_,
        )
        return self

    def predict_proba(self, X) -> np.ndarray:
        """Calibrated ``p(y=1 | x)`` as an (n, 2) array of [p(y=0), p(y=1)]."""
        X = np.asarray(X, dtype=np.float64)
        g = self._pos_proba(self.estimator_, X)
        if self.method == "calibrated":
            p1 = np.clip(g / self.c_, 0.0, 1.0)
        else:
            p1 = g  # the weighted refit already models y directly
        return np.column_stack([1.0 - p1, p1])

    def predict(self, X, threshold: float = 0.5) -> np.ndarray:
        return (self.predict_proba(X)[:, 1] >= threshold).astype(int)


# ---------------------------------------------------------------------------
# Data Cube integration
# ---------------------------------------------------------------------------

_REDUCERS = {
    "mean": np.nanmean,
    "std": np.nanstd,
    "median": np.nanmedian,
    "min": np.nanmin,
    "max": np.nanmax,
}


@dataclass
class PUFeatureMeta:
    """Recipe + geometry needed to reproduce the feature matrix on new data."""
    feature_names: list[str]
    reducers: tuple
    shape: tuple            # (y, x) of the source grid
    valid_index: np.ndarray = field(repr=False)  # flat indices of valid pixels


def _cube_features(cube, reducers: tuple) -> tuple[np.ndarray, list[str], tuple]:
    """(n_pixels, n_features) matrix from a (time, band, y, x) or (band, y, x)
    DataArray by reducing over time; rows are y*x flattened pixels."""
    import warnings as _warnings

    data = np.asarray(cube.data if hasattr(cube, "data") else cube,
                      dtype=np.float64)
    if data.ndim == 3:            # (band, y, x): static — reducers collapse
        data = data[None, ...]    # to the values themselves via a 1-el time axis
    if data.ndim != 4:
        raise ValueError(
            f"cube must be (time, band, y, x) or (band, y, x); got {data.shape}"
        )
    _, n_band, ny, nx = data.shape
    band_names = (
        [str(b) for b in np.asarray(cube.coords["band"].values)]
        if hasattr(cube, "coords") and "band" in getattr(cube, "coords", {})
        else [f"band{i}" for i in range(n_band)]
    )
    cols, names = [], []
    with _warnings.catch_warnings():
        _warnings.filterwarnings("ignore", "All-NaN slice")
        _warnings.filterwarnings("ignore", "Mean of empty slice")
        _warnings.filterwarnings("ignore", "Degrees of freedom")
        for bi, bname in enumerate(band_names):
            for red in reducers:
                fn = _REDUCERS.get(red)
                if fn is None:
                    raise ValueError(
                        f"Unknown reducer {red!r}; choose from "
                        f"{sorted(_REDUCERS)}"
                    )
                cols.append(fn(data[:, bi], axis=0).reshape(-1))
                names.append(f"{bname}_{red}")
    return np.column_stack(cols), names, (ny, nx)


def pu_training_set(
    cube,
    positive_mask: np.ndarray,
    *,
    reducers: tuple = ("mean", "std"),
    unlabeled_per_positive: float | None = None,
    random_state: int | None = None,
) -> tuple[np.ndarray, np.ndarray, PUFeatureMeta]:
    """Build a PU training matrix from a Data Cube and a 2-D positive mask.

    Features are per-pixel temporal reductions of each band (default: mean and
    std over time — the classic backscatter level + variability pair). Pixels
    with any non-finite feature are dropped. Unlabeled pixels may be
    subsampled to ``unlabeled_per_positive`` × the positive count (SAR scenes
    have millions of unlabeled pixels; a few tens per positive is plenty).

    Returns ``(X, s, meta)``: features, PU labels (1 = positive-labeled,
    0 = unlabeled), and the :class:`PUFeatureMeta` needed by
    :func:`predict_proba_map`.
    """
    feats, names, shape = _cube_features(cube, tuple(reducers))
    mask = np.asarray(positive_mask, dtype=bool)
    if mask.shape != shape:
        raise ValueError(
            f"positive_mask shape {mask.shape} != cube grid {shape}"
        )
    valid = np.isfinite(feats).all(axis=1)
    pos = mask.reshape(-1) & valid
    unl = ~mask.reshape(-1) & valid
    n_pos = int(pos.sum())
    if n_pos == 0:
        raise ValueError("positive_mask selects no valid pixels")

    rng = np.random.default_rng(random_state)
    unl_idx = np.flatnonzero(unl)
    if unlabeled_per_positive is not None:
        n_keep = min(len(unl_idx), int(round(unlabeled_per_positive * n_pos)))
        unl_idx = rng.choice(unl_idx, size=n_keep, replace=False)
    pos_idx = np.flatnonzero(pos)

    sel = np.concatenate([pos_idx, unl_idx])
    X = feats[sel]
    s = np.concatenate([np.ones(len(pos_idx), int), np.zeros(len(unl_idx), int)])
    meta = PUFeatureMeta(
        feature_names=names, reducers=tuple(reducers), shape=shape,
        valid_index=np.flatnonzero(valid),
    )
    logger.info(
        "[PU] training set: %d positive / %d unlabeled pixels, %d features (%s)",
        len(pos_idx), len(unl_idx), X.shape[1], ", ".join(names),
    )
    return X, s, meta


def predict_proba_map(
    clf: PUClassifier,
    cube,
    meta: PUFeatureMeta | None = None,
    *,
    reducers: tuple | None = None,
) -> np.ndarray:
    """Paint calibrated ``p(y=1)`` onto the cube's (y, x) grid.

    Pixels with any non-finite feature come back NaN. ``meta`` (from
    :func:`pu_training_set`) supplies the reducer recipe; pass ``reducers``
    explicitly when scoring a cube that was not used for training.
    """
    red = tuple(reducers) if reducers is not None else (
        meta.reducers if meta is not None else ("mean", "std")
    )
    feats, _names, shape = _cube_features(cube, red)
    valid = np.isfinite(feats).all(axis=1)
    out = np.full(shape[0] * shape[1], np.nan, dtype=np.float32)
    if valid.any():
        out[valid] = clf.predict_proba(feats[valid])[:, 1].astype(np.float32)
    return out.reshape(shape)
