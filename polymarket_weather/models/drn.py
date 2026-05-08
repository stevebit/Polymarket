"""Distributional Regression Network (stretch goal — stub).

DRN (Rasp & Lerch 2018) is a small MLP that maps a feature vector
``(forecast_mean, forecast_spread, climatology_mean, climatology_std,
lead_day, doy_sin, doy_cos, today_so_far_max)`` to two outputs ``(mu, log_sigma)``,
trained on the negative log-likelihood of a Gaussian predictive distribution.

This module is a **stub**: the import is lazy so the optional dependency on
PyTorch never blocks the rest of the pipeline. When the user installs torch
and runs ``cli.fit_drn`` (not yet implemented), DRN coefficients land in the
same ``postprocess_coefs`` table with ``model_id='drn:v1'``. Until then, M2
falls back to EMOS.

To enable later:

1. ``pip install torch``
2. Implement ``fit_drn`` as a small MLP minimising NLL on the same training
   pairs assembled by :func:`postprocess._fetch_training_pairs`, but with
   richer features.
3. Persist either coefficients or a serialized state_dict; extend
   ``apply_drn`` to load and infer.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

DRN_MODEL_ID = "drn:v1"


def fit_drn(*args, **kwargs):  # noqa: ANN001
    """Not implemented in MVP. Plan documents the architecture; flip
    when torch is available."""
    raise NotImplementedError(
        "DRN training not implemented. EMOS is the production postprocessor."
    )


def apply_drn(*args, **kwargs):  # noqa: ANN001
    raise NotImplementedError(
        "DRN inference not implemented. Use models.postprocess.apply_emos instead."
    )
