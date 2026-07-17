"""Positive-Unlabeled learning (s1grits.analysis.pu_learning).

Locks the Elkan–Noto contract on synthetic problems where the ground truth
is KNOWN: the label frequency estimate c_ recovers the true labelling rate,
calibrated probabilities approach the true posterior (which a naive
positive-vs-unlabeled classifier systematically underestimates by the factor
c), the class prior is recovered, the weighted variant agrees, and the Data
Cube helpers round-trip a (time, band, y, x) cube into a probability map.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("sklearn")

from s1grits.analysis.pu_learning import (  # noqa: E402
    PUClassifier,
    predict_proba_map,
    pu_training_set,
)


def _pu_problem(n=6000, c=0.4, prior=0.3, seed=0):
    """Well-separated 2-D Gaussians; positives labeled at rate c (SCAR).

    The e1 estimator (mean g over held-out positives) assumes positives score
    near 1 under the true posterior — Elkan & Noto's separability premise —
    so the clusters are placed ~6 sigma apart. With heavy class overlap e1 is
    known to underestimate c by the factor E[p(y=1|x) | y=1].
    """
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < prior).astype(int)
    X = np.where(
        y[:, None] == 1,
        rng.normal(+2.5, 0.8, (n, 2)),
        rng.normal(-2.5, 0.8, (n, 2)),
    )
    s = np.where((y == 1) & (rng.random(n) < c), 1, 0)
    return X, y, s


def test_estimates_label_frequency_and_prior():
    X, y, s = _pu_problem(c=0.4, prior=0.3)
    clf = PUClassifier(random_state=0).fit(X, s)
    assert clf.c_ == pytest.approx(0.4, abs=0.08)
    assert clf.prior_ == pytest.approx(0.3, abs=0.08)


def test_calibrated_probability_beats_naive_pvsu():
    """The naive P-vs-U score approximates c * p(y=1|x); dividing by c_ must
    bring positives' mean probability close to 1 where the naive score sits
    near c."""
    X, y, s = _pu_problem(c=0.4, prior=0.3, seed=1)
    clf = PUClassifier(random_state=1).fit(X, s)
    p_cal = clf.predict_proba(X[y == 1])[:, 1].mean()
    p_naive = clf._pos_proba(clf.estimator_, X[y == 1]).mean()
    assert p_naive < 0.6            # naive is crushed toward c
    assert p_cal > 0.85             # calibration recovers the posterior
    # separable problem: calibrated decisions recover true labels well
    acc = (clf.predict(X) == y).mean()
    assert acc > 0.9


def test_weighted_method_agrees_with_calibrated():
    X, y, s = _pu_problem(c=0.5, prior=0.35, seed=2)
    cal = PUClassifier(method="calibrated", random_state=2).fit(X, s)
    wgt = PUClassifier(method="weighted", random_state=2).fit(X, s)
    acc_cal = (cal.predict(X) == y).mean()
    acc_wgt = (wgt.predict(X) == y).mean()
    assert acc_wgt > 0.9 and abs(acc_wgt - acc_cal) < 0.05
    assert wgt.c_ == pytest.approx(0.5, abs=0.1)


def test_known_c_skips_estimation():
    X, y, s = _pu_problem(c=0.4, seed=3)
    clf = PUClassifier(c=0.4, random_state=3).fit(X, s)
    assert clf.c_ == 0.4


def test_input_validation():
    X, _, s = _pu_problem(n=200, seed=4)
    with pytest.raises(ValueError, match="calibrated"):
        PUClassifier(method="nope")
    with pytest.raises(ValueError, match="only 0"):
        PUClassifier().fit(X, np.full(len(X), 2))
    with pytest.raises(ValueError, match="both labeled positives"):
        PUClassifier().fit(X, np.zeros(len(X)))
    with pytest.raises(ValueError, match="hold_out_ratio"):
        PUClassifier(hold_out_ratio=1.5)


# ---------------------------------------------------------------------------
# Data Cube integration
# ---------------------------------------------------------------------------

def _cube(ny=40, nx=50, nt=8, seed=5):
    """xarray (time, band, y, x) cube: positives are a bright block in VV."""
    xr = pytest.importorskip("xarray")
    rng = np.random.default_rng(seed)
    truth = np.zeros((ny, nx), bool)
    truth[8:24, 10:30] = True
    vv = rng.normal(-14, 1.0, (nt, ny, nx))
    vv[:, truth] += 5.0
    vh = rng.normal(-20, 1.0, (nt, ny, nx)) + 0.3 * (vv + 14)
    data = np.stack([vv, vh], axis=1).astype(np.float32)
    data[:, :, :2, :] = np.nan  # invalid margin rows
    cube = xr.DataArray(
        data, dims=("time", "band", "y", "x"),
        coords={"band": ["VV_dB", "VH_dB"]},
    )
    return cube, truth


def test_cube_training_set_and_probability_map():
    cube, truth = _cube()
    rng = np.random.default_rng(6)
    # SCAR labels: 40% of true positives marked
    positive_mask = truth & (rng.random(truth.shape) < 0.4)

    X, s, meta = pu_training_set(
        cube, positive_mask, unlabeled_per_positive=5, random_state=6,
    )
    assert X.shape[1] == 4  # 2 bands x (mean, std)
    assert meta.feature_names == ["VV_dB_mean", "VV_dB_std",
                                  "VH_dB_mean", "VH_dB_std"]
    assert s.sum() == int((positive_mask & ~np.isnan(
        np.asarray(cube[0, 0]))).sum())

    clf = PUClassifier(random_state=6).fit(X, s)
    prob = predict_proba_map(clf, cube, meta)
    assert prob.shape == truth.shape
    assert np.isnan(prob[:2, :]).all()          # invalid margin stays NaN
    valid = ~np.isnan(prob)
    # the recovered map separates true positives from the rest
    assert np.nanmean(prob[truth & valid]) > 0.8
    assert np.nanmean(prob[(~truth) & valid]) < 0.2
    # decision map recovers the full block, not just the 40% labeled
    recovered = (prob > 0.5) & valid
    hit = (recovered & truth).sum() / truth[valid].sum()
    assert hit > 0.9


def test_cube_mask_shape_mismatch_raises():
    cube, truth = _cube(ny=10, nx=12, nt=3)
    with pytest.raises(ValueError, match="positive_mask shape"):
        pu_training_set(cube, np.zeros((5, 5), bool))
