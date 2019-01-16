#!/usr/bin/env python3

from copy import deepcopy
from time import time
from typing import Callable, Dict, List, Optional, Tuple

import gpytorch
import torch
from gpytorch.mlls.exact_marginal_log_likelihood import ExactMarginalLogLikelihood
from torch import Tensor

from .. import fit_model
from ..acquisition.batch_modules import BatchAcquisitionFunction
from ..acquisition.utils import get_acquisition_function
from ..gen import gen_candidates_scipy, get_best_candidates
from ..models.model import Model
from ..optim.initializers import get_similarity_measure, initialize_q_batch
from ..utils import draw_sobol_samples
from .config import AcquisitionFunctionConfig, OptimizeConfig
from .output import BenchmarkOutput, ClosedLoopOutput, _ModelBestPointOutput


def _get_fitted_model(
    train_X: Tensor, train_Y: Tensor, train_Y_se: Tensor, model: Model, maxiter: int
) -> Model:
    """
    Helper function that returns a model fitted to the provided data.

    Args:
        train_X: A `n x d` (or `b x n x d`) Tensor of points
        train_Y: A `n x (t)` (or `b x n x (t)`) Tensor of outcomes
        train_Y_se: A `b x n x (t)` (or `b x n x (t)`) Tensor of observed standard
            errors for each outcome
        model: an initialized Model. This model must have a likelihood attribute.
        maxiter: The maximum number of iterations
    Returns:
        Model: a fitted model
    """
    # TODO: copy over the state_dict from the existing model before
    # optimization begins
    model.reinitialize(train_X=train_X, train_Y=train_Y, train_Y_se=train_Y_se)
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    mll.to(dtype=train_X.dtype, device=train_X.device)
    mll = fit_model(mll, options={"maxiter": maxiter})
    return model


def greedy(
    X: Tensor,
    model: Model,
    objective: Callable[[Tensor], Tensor] = lambda Y: Y,
    constraints: Optional[List[Callable[[Tensor], Tensor]]] = None,
    mc_samples: int = 10000,
) -> Tuple[Tensor, Tensor, Tensor]:
    """
    Fetch the best point, best objective, and feasibility based on the joint
    posterior of the evaluated points.

    Args:
        X: `q x d` (or `b x q x d`)-dim (batch mode) tensor of points
        model: model: A fitted model.
        objective: A callable mapping a Tensor of size `b x q x (t)` to a Tensor
            of size `b x q`, where `t` is the number of outputs (tasks) of the
            model. Note: the callable must support broadcasting. If omitted, use
            the identity map (applicable to single-task models only).
            Assumed to be non-negative when the constraints are used!
        constraints: A list of callables, each mapping a Tensor of size
            `b x q x t` to a Tensor of size `b x q`, where negative values imply
            feasibility. Note: the callable must support broadcasting. Only
            relevant for multi-task models (`t` > 1).
        mc_samples: The number of Monte-Carlo samples to draw from the model
            posterior.
    Returns:
        Tensor: `d` (or `b x d`)-dim (batch mode) best point(s)
        Tensor: `0` (or `b`)-dim (batch mode) tensor of best objective(s)
        Tensor: `0` (or `b`)-dim (batch mode) tensor of feasibility of best point(s)

    """
    if X.dim() < 2 or X.dim() > 3:
        raise ValueError("X must have two or three dimensions")
    batch_mode = X.dim() == 3
    if not batch_mode:
        X = X.unsqueeze(0)  # internal logic always operates in batch mode
    with torch.no_grad(), gpytorch.settings.fast_pred_var():
        posterior = model.posterior(X)
        # mc_samples x b x q x (t)
        samples = posterior.rsample(sample_shape=torch.Size([mc_samples]))
    # TODO: handle non-positive definite objectives
    obj = objective(samples).clamp_min_(0)  # pyre-ignore [16]
    obj_raw = objective(samples)
    feas_raw = torch.ones_like(obj_raw)
    if constraints is not None:
        for constraint in constraints:
            feas_raw.mul_((constraint(samples) < 0).type_as(obj))  # pyre-ignore [16]
        obj.mul_(feas_raw)
    # get max index of weighted objective along the q-batch dimension
    _, best_idcs = torch.max(obj.mean(dim=0), dim=-1)
    # extract corresponding values of X, raw objective, and feasibility
    batch_idxr = torch.arange(best_idcs.numel(), device=best_idcs.device)
    X_best = X[batch_idxr, best_idcs, :].detach()
    obj_best = obj_raw.mean(dim=0)[batch_idxr, best_idcs]
    feas_best = feas_raw.mean(dim=0)[batch_idxr, best_idcs]
    if not batch_mode:
        # squeeze dimensions back if not called in batch mode
        X_best = X_best.squeeze(0)
        obj_best = obj_best.squeeze(0)
        feas_best = feas_best.squeeze(0)
    return X_best, obj_best, feas_best


def _fit_model_and_get_best_point(
    train_X: Tensor,
    train_Y: Tensor,
    train_Y_se: Tensor,
    model: Model,
    max_retries: int,
    maxiter: int,
    verbose: bool,
    objective: Callable[[Tensor], Tensor] = lambda Y: Y,
    constraints: Optional[List[Callable[[Tensor], Tensor]]] = None,
    retry: int = 0,
) -> _ModelBestPointOutput:
    """
    Fit model and fetch the best point, best objective, and feasibility based on
    the joint posterior of the evaluated points.

    Args:
        train_X: A `n x d` Tensor of points
        train_Y: A `n x (t)` Tensor of outcomes
        train_Y_se: A `n x (t)` Tensor of observed standard errors for each outcome
        model: An initialized Model. This model must have a likelihood attribute.
        max_retries: Themaximum number of retries
        maxiter: The maximum number of iterations
        verbose: whether to provide verbose output
        objective: A callable mapping a Tensor of size `b x q x (t)` to a Tensor
            of size `b x q`, where `t` is the number of outputs (tasks) of the
            model. Note: the callable must support broadcasting. If omitted, use
            the identity map (applicable to single-task models only).
            Assumed to be non-negative when the constraints are used!
        constraints: A list of callables, each mapping a Tensor of size
            `b x q x t` to a Tensor of size `b x q`, where negative values imply
            feasibility. Note: the callable must support broadcasting. Only
            relevant for multi-task models (`t` > 1).
        retry: current retry count
    Returns:
        _ModelBestPointOutput: container with:
            - model (Model): a fitted model
            - best_point (Tensor): `d` (or `b x d`)-dim (batch mode) best point(s)
            - obj (Tensor): `0` (or `b`)-dim (batch mode) tensor of best objective(s)
            - feas (Tensor): `0` (or `b`)-dim (batch mode) tensor of feasibility of best point(s)
            - retry (int): the current retry count

    """
    # handle errors in model fitting and evaluation (when selecting best point)
    while retry <= max_retries:
        try:
            if verbose:
                print("---- train")
            model = _get_fitted_model(
                train_X=train_X,
                train_Y=train_Y,
                train_Y_se=train_Y_se,
                model=model,
                maxiter=maxiter,
            )
            if verbose:
                print("---- identify")
            best_point, obj, feas = greedy(
                X=train_X, model=model, objective=objective, constraints=constraints
            )
            return _ModelBestPointOutput(
                model=model, best_point=best_point, obj=obj, feas=feas, retry=retry
            )
        except Exception:
            retry += 1
            if verbose:
                print(f"---- Failed {retry} times ----")
            if retry > max_retries:
                raise


def run_closed_loop(
    func: Callable[[Tensor], List[Tensor]],
    acq_func_config: AcquisitionFunctionConfig,
    optim_config: OptimizeConfig,
    model: Model,
    output: ClosedLoopOutput,
    lower_bounds: Tensor,
    upper_bounds: Tensor,
    objective: Callable[[Tensor], Tensor] = lambda Y: Y,
    constraints: Optional[List[Callable[[Tensor], Tensor]]] = None,
    verbose: bool = False,
    seed: Optional[int] = None,
) -> ClosedLoopOutput:
    """
    Uses Bayesian Optimization to optimize func.

    Args:
        func: function to optimize (maximize by default)
        acq_func_config: configuration for the acquisition function
        optim_config: configuration for the optimization
        model: an initialized Model. This model must have a likelihood attribute.
        output: a ClosedLoopOutput containing the initial points and observations
            and the best point, objective, and feasibility from the initial batch.
        objective: A callable mapping a Tensor of size `b x q x (t)`
            to a Tensor of size `b x q`, where `t` is the number of
            outputs (tasks) of the model. Note: the callable must support broadcasting.
            If omitted, use the identity map (applicable to single-task models only).
            Assumed to be non-negative when the constraints are used!
        constraints: A list of callables, each mapping a Tensor of size
            `b x q x t` to a Tensor of size `b x q`, where negative values imply
            feasibility. Note: the callable must support broadcasting. Only
            relevant for multi-task models (`t` > 1).
        lower_bounds: minimum values for each column of initial_candidates
        upper_bounds: maximum values for each column of initial_candidates
        verbose: whether to provide verbose output
        seed: if seed is provided, do deterministic optimization where the function to
            optimize is fixed and not stochastic. This seed is also used to make
            sampling the initial conditions deterministic.

    Returns:
        ClosedLoopOutput: results of closed loop

    # TODO: Add support for multi-task models.
    """
    # TODO: remove exception handling wrappers when model fitting is stabilized
    retry = 0
    train_X = torch.cat(output.Xs, dim=0)
    train_Y = torch.cat(output.Ys, dim=0)
    train_Y_se = torch.cat(output.Ycovs, dim=0).sqrt()
    if train_X.dim() < 2 or train_X.dim() > 3:
        raise ValueError(
            "Xs must contain tensors with two or three dimensions (batch_mode)"
        )
    batch_mode = train_X.dim() == 3
    for iteration in range(optim_config.n_batch):
        start_time = time()
        if verbose:
            print("Iteration:", iteration + 1)
        failed = True
        while retry <= optim_config.max_retries and failed:
            try:
                acq_func: BatchAcquisitionFunction = get_acquisition_function(
                    acquisition_function_name=acq_func_config.name,
                    model=model,
                    X_observed=train_X,
                    objective=objective,
                    constraints=constraints,
                    X_pending=None,
                    seed=seed,
                    acquisition_function_args=acq_func_config.args,
                )
                if verbose:
                    print("---- acquisition optimization")

                # Generate random points for determining intial conditions
                X_rnd = draw_sobol_samples(
                    bounds=torch.stack([lower_bounds, upper_bounds]),
                    n=optim_config.num_raw_samples,
                    q=optim_config.q,
                    seed=seed,
                )
                with torch.no_grad():
                    Y_rnd = acq_func(X_rnd)

                sim_measure = get_similarity_measure(model=model)
                batch_initial_conditions = initialize_q_batch(
                    X=X_rnd,
                    Y=Y_rnd,
                    n=optim_config.num_starting_points,
                    sim_measure=sim_measure,
                )

                batch_candidates, batch_acq_values = gen_candidates_scipy(
                    initial_candidates=batch_initial_conditions,
                    acquisition_function=acq_func,
                    lower_bounds=lower_bounds,
                    upper_bounds=upper_bounds,
                )
                candidates = get_best_candidates(
                    batch_candidates=batch_candidates, batch_values=batch_acq_values
                )

                X = acq_func.extract_candidates(candidates).detach()
                failed = False
            except Exception:
                retry += 1
                if verbose:
                    print("---- Failed {} times ----".format(retry))
                # The exception occured during evaluation time, so refit the model
                # and recompute the best point using the model
                model_and_best_point_output = _fit_model_and_get_best_point(
                    train_X=train_X,
                    train_Y=train_Y,
                    train_Y_se=train_Y_se,
                    model=model,
                    max_retries=optim_config.max_retries,
                    maxiter=optim_config.model_maxiter,
                    verbose=verbose,
                    objective=objective,
                    constraints=constraints,
                    retry=retry,
                )
                model = model_and_best_point_output.model
                retry = model_and_best_point_output.retry
                # overwrite last stored best point since this is the same iteration
                best_point = (
                    model_and_best_point_output.best_point
                    if batch_mode
                    else model_and_best_point_output.best_point.unsqueeze(0)
                )
                output.best[-1] = best_point
                output.best_model_objective[-1] = model_and_best_point_output.obj
                output.best_model_feasibility[-1] = model_and_best_point_output.feas
                output.costs[-1] = 1.0
                if retry > optim_config.max_retries:
                    raise
        if verbose:
            print("---- evaluate")
        Y, Ycov = func(X)
        output.Xs.append(X)
        output.Ys.append(Y)
        output.Ycovs.append(Ycov)
        train_X = torch.cat(output.Xs, dim=0)
        train_Y = torch.cat(output.Ys, dim=0)
        train_Y_se = torch.cat(output.Ycovs, dim=0).sqrt()
        model_and_best_point_output = _fit_model_and_get_best_point(
            train_X=train_X,
            train_Y=train_Y,
            train_Y_se=train_Y_se,
            model=model,
            max_retries=optim_config.max_retries,
            maxiter=optim_config.model_maxiter,
            verbose=verbose,
            objective=objective,
            constraints=constraints,
            retry=retry,
        )
        model = model_and_best_point_output.model
        retry = model_and_best_point_output.retry
        best_point = (
            model_and_best_point_output.best_point
            if batch_mode
            else model_and_best_point_output.best_point.unsqueeze(0)
        )
        output.best.append(best_point)
        output.best_model_objective.append(model_and_best_point_output.obj)
        output.best_model_feasibility.append(model_and_best_point_output.feas)
        output.costs.append(1.0)
        output.runtimes.append(time() - start_time)
    return output


def run_benchmark(
    func: Callable[[Tensor], List[Tensor]],
    acq_func_configs: Dict[str, AcquisitionFunctionConfig],
    optim_config: OptimizeConfig,
    initial_model: Model,
    lower_bounds: Tensor,
    upper_bounds: Tensor,
    objective: Callable[[Tensor], Tensor] = lambda Y: Y,
    constraints: Optional[List[Callable[[Tensor], Tensor]]] = None,
    verbose: bool = False,
    seed: Optional[int] = None,
    num_runs: Optional[int] = 1,
    true_func: Optional[Callable[[Tensor], List[Tensor]]] = None,
    global_optimum: Optional[float] = None,
) -> Dict[str, BenchmarkOutput]:
    """
    Uses Bayesian Optimization to optimize func multiple times.

    Args:
        func: function to optimize (maximize by default)
        acq_func_configs: dictionary mapping names to configurations for the
            acquisition functions
        optim_config: configuration for the optimization
        model: an initialized Model. This model must have a likelihood attribute.
        objective: A callable mapping a Tensor of size `b x q x (t)`
            to a Tensor of size `b x q`, where `t` is the number of
            outputs (tasks) of the model. Note: the callable must support broadcasting.
            If omitted, use the identity map (applicable to single-task models only).
            Assumed to be non-negative when the constraints are used!
        constraints: A list of callables, each mapping a Tensor of size
            `b x q x t` to a Tensor of size `b x q`, where negative values imply
            feasibility. Note: the callable must support broadcasting. Only
            relevant for multi-task models (`t` > 1).
        lower_bounds: minimum values for each column of initial_candidates
        upper_bounds: maximum values for each column of initial_candidates
        verbose: whether to provide verbose output
        seed: if seed is provided, do deterministic optimization where the function to
            optimize is fixed and not stochastic. This seed is also used to make
            sampling the initial conditions deterministic.
        num_runs: number of runs of bayesian optimization
        true_func: true noiseless function being optimized
        global_optimum: the global optimum of func after applying the objective
            transformation. If provided, this is used to compute regret.
    Returns:
        Dict[str, BenchmarkOutput]: dictionary mapping each key of acq_func_configs
            to its corresponding output.
    """
    outputs = {
        method_name: BenchmarkOutput(
            Xs=[],
            Ys=[],
            Ycovs=[],
            best=[],
            best_model_objective=[],
            best_model_feasibility=[],
            costs=[],
            runtimes=[],
            best_true_objective=[],
            best_true_feasibility=[],
            regrets=[],
            cumulative_regrets=[],
            weights=[],
        )
        for method_name in acq_func_configs
    }
    for run in range(num_runs):
        start_time = time()
        X = draw_sobol_samples(
            bounds=torch.stack([lower_bounds, upper_bounds]),
            n=1,
            q=optim_config.initial_points,
            seed=seed,
        ).squeeze(0)
        Y, Ycov = func(X)
        model_and_best_point_output = _fit_model_and_get_best_point(
            train_X=X,
            train_Y=Y,
            train_Y_se=Ycov.sqrt(),
            model=deepcopy(initial_model),
            max_retries=optim_config.max_retries,
            maxiter=optim_config.model_maxiter,
            verbose=verbose,
            objective=objective,
            constraints=constraints,
        )
        runtime = time() - start_time
        for (method_name, acq_func_config) in acq_func_configs.items():
            if verbose:
                print(f"---- Starting loop {run + 1} with {acq_func_config.name} ----")
            acquisition_function_output = outputs[method_name]
            run_output = ClosedLoopOutput(
                Xs=[X],
                Ys=[Y],
                Ycovs=[Ycov],
                best=[model_and_best_point_output.best_point.unsqueeze(0)],
                best_model_objective=[model_and_best_point_output.obj],
                best_model_feasibility=[model_and_best_point_output.feas],
                costs=[1.0],
                runtimes=[runtime],
            )
            run_output = run_closed_loop(
                func=func,
                acq_func_config=acq_func_config,
                optim_config=optim_config,
                model=deepcopy(model_and_best_point_output.model),
                output=run_output,
                lower_bounds=lower_bounds,
                upper_bounds=upper_bounds,
                objective=objective,
                constraints=constraints,
                verbose=verbose,
                seed=seed,
            )
            if verbose:
                print(f"---- Finished {run + 1} loops with {acq_func_config.name} ----")
            # compute true objective base on best point (greedy from model)
            best = torch.cat(run_output.best, dim=0)
            if true_func is not None:
                f_best = true_func(best)[0]
                best_true_objective = objective(f_best).view(-1)
                best_true_feasibility = torch.ones_like(best_true_objective)
                if constraints is not None:
                    for constraint in constraints:
                        best_true_feasibility.mul_(  # pyre-ignore [16]
                            (constraint(f_best) < 0)  # pyre-ignore [16]
                            .type_as(best)
                            .view(-1)
                        )
                acquisition_function_output.best_true_objective.append(
                    best_true_objective
                )
                acquisition_function_output.best_true_feasibility.append(
                    best_true_feasibility
                )
                if global_optimum is not None:
                    regrets = torch.tensor(
                        [
                            (
                                global_optimum - objective(true_func(X)[0])
                            )  # pyre-ignore [6]
                            .sum()
                            .item()
                            for X in run_output.Xs
                        ]
                    ).type_as(best)
                    # check that objective is never > than global_optimum
                    assert torch.all(regrets >= 0)
                    # compute regret on best point (greedy from model)
                    cumulative_regrets = torch.cumsum(regrets, dim=0)
                    acquisition_function_output.regrets.append(regrets)
                    acquisition_function_output.cumulative_regrets.append(
                        cumulative_regrets
                    )
            for f in run_output._fields:
                getattr(acquisition_function_output, f).append(getattr(run_output, f))
        if seed is not None:
            seed += 1
    return outputs
