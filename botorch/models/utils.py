#! /usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

r"""
Utiltiy functions for models.
"""

import warnings
from typing import List, Optional, Tuple

import torch
from gpytorch.utils.broadcasting import _mul_broadcast_shape
from torch import Tensor

from ..exceptions import InputDataError, InputDataWarning


def _make_X_full(X: Tensor, output_indices: List[int], tf: int) -> Tensor:
    r"""Helper to construct input tensor with task indices.

    Args:
        X: The raw input tensor (without task information).
        output_indices: The output indices to generate (passed in via `posterior`).
        tf: The task feature index.

    Returns:
        Tensor: The full input tensor for the multi-task model, including task
            indices.
    """
    index_shape = X.shape[:-1] + torch.Size([1])
    indexers = (
        torch.full(index_shape, fill_value=i, device=X.device, dtype=X.dtype)
        for i in output_indices
    )
    X_l, X_r = X[..., :tf], X[..., tf:]
    return torch.cat(
        [torch.cat([X_l, indexer, X_r], dim=-1) for indexer in indexers], dim=0
    )


def multioutput_to_batch_mode_transform(
    train_X: Tensor,
    train_Y: Tensor,
    num_outputs: int,
    train_Yvar: Optional[Tensor] = None,
) -> Tuple[Tensor, Tensor, Optional[Tensor]]:
    r"""Transforms training inputs for a multi-output model.

    Used for multi-output models that internally are represented by a
    batched single output model, where each output is modeled as an
    independent batch.

    Args:
        train_X: A `n x d` or `input_batch_shape x n x d` (batch mode) tensor of
            training features.
        train_Y: A `n x m` or `target_batch_shape x n x m` (batch mode) tensor of
            training observations.
        num_outputs: number of outputs
        train_Yvar: A `n x m` or `target_batch_shape x n x m` tensor of observed
            measurement noise.

    Returns:
        3-element tuple containing

        - A `input_batch_shape x m x n x d` tensor of training features.
        - A `target_batch_shape x m x n` tensor of training observations.
        - A `target_batch_shape x m x n` tensor observed measurement noise.
    """
    # make train_Y `batch_shape x m x n`
    train_Y = train_Y.transpose(-1, -2)
    # expand train_X to `batch_shape x m x n x d`
    train_X = train_X.unsqueeze(-3).expand(
        train_X.shape[:-2] + torch.Size([num_outputs]) + train_X.shape[-2:]
    )
    if train_Yvar is not None:
        # make train_Yvar `batch_shape x m x n`
        train_Yvar = train_Yvar.transpose(-1, -2)
    return train_X, train_Y, train_Yvar


def add_output_dim(X: Tensor, original_batch_shape: torch.Size) -> Tuple[Tensor, int]:
    r"""Insert the output dimension at the correct location.

    The trailing batch dimensions of X must match the original batch dimensions
    of the training inputs, but can also include extra batch dimensions.

    Args:
        X: A `(new_batch_shape) x (original_batch_shape) x n x d` tensor of features.
        original_batch_shape: the batch shape of the model's training inputs.

    Returns:
        2-element tuple containing

        - A `(new_batch_shape) x (original_batch_shape) x m x n x d` tensor of
        features.
        - The index corresponding to the output dimension.
    """
    X_batch_shape = X.shape[:-2]
    if len(X_batch_shape) > 0 and len(original_batch_shape) > 0:
        # check that X_batch_shape supports broadcasting or augments
        # original_batch_shape with extra batch dims
        error_msg = (
            "The trailing batch dimensions of X must match the trailing "
            "batch dimensions of the training inputs."
        )
        _mul_broadcast_shape(X_batch_shape, original_batch_shape, error_msg=error_msg)
    # insert `m` dimension
    X = X.unsqueeze(-3)
    output_dim_idx = max(len(original_batch_shape), len(X_batch_shape))
    return X, output_dim_idx


def check_no_nans(Z: Tensor) -> None:
    r"""Check that tensor does not contain NaN values.

    Raises an InputDataError if `Z` contains NaN values.

    Args:
        Z: The input tensor.
    """
    if torch.any(torch.isnan(Z)).item():
        raise InputDataError("Input data contains NaN values.")


def check_min_max_scaling(
    X: Tensor, strict: bool = False, atol: float = 1e-2, raise_on_fail: bool = False
) -> None:
    r"""Check that tensor is normalized to the unit cube.

    Args:
        X: A `batch_shape x n x d` input tensor. Typically the training inputs
            of a model.
        strict: If True, require `X` to be scaled to the unit cube (rather than
            just to be contained within the unit cube).
        atol: The tolerance for the boundary check. Only used if `strict=True`.
        raise_on_fail: If True, raise an exception instead of a warning.
    """
    with torch.no_grad():
        Xmin, Xmax = torch.min(X, dim=-1)[0], torch.max(X, dim=-1)[0]
        msg = None
        if strict and max(torch.abs(Xmin).max(), torch.abs(Xmax - 1).max()) > atol:
            msg = "scaled"
        if torch.any(Xmin < -atol) or torch.any(Xmax > 1 + atol):
            msg = "contained"
        if msg is not None:
            msg = (
                f"Input data is not {msg} to the unit cube. "
                "Please consider min-max scaling the input data."
            )
            if raise_on_fail:
                raise InputDataError(msg)
            warnings.warn(msg, InputDataWarning)


def check_standardization(
    Y: Tensor,
    atol_mean: float = 1e-2,
    atol_std: float = 1e-2,
    raise_on_fail: bool = False,
) -> None:
    r"""Check that tensor is standardized (zero mean, unit variance).

    Args:
        Y: The input tensor of shape `batch_shape x n x m`. Typically the
            train targets of a model. Standardization is checked across the
            `n`-dimension.
        atol_mean: The tolerance for the mean check.
        atol_std: The tolerance for the std check.
        raise_on_fail: If True, raise an exception instead of a warning.
    """
    with torch.no_grad():
        Ymean, Ystd = torch.mean(Y, dim=-2), torch.std(Y, dim=-2)
        if torch.abs(Ymean).max() > atol_mean or torch.abs(Ystd - 1).max() > atol_std:
            msg = (
                "Input data is not standardized. Please consider scaling the "
                "input to zero mean and unit variance."
            )
            if raise_on_fail:
                raise InputDataError(msg)
            warnings.warn(msg, InputDataWarning)
