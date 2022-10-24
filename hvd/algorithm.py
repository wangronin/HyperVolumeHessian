import logging
import time
from typing import Callable, Dict, List, Tuple, Union

import numpy as np
from scipy.linalg import block_diag, cholesky
from scipy.sparse import csc_matrix
from scipy.sparse.linalg import spsolve
from scipy.spatial.distance import cdist

from .hypervolume import hypervolume
from .hypervolume_derivatives import HypervolumeDerivatives
from .logger import get_logger
from .utils import non_domin_sort, set_bounds

__authors__ = ["Hao Wang"]


class HVN:
    """Hypervolume Newton method

    Newton-Raphson method applied to maximize the hypervolume indicator
    """

    def __init__(
        self,
        dim: int,
        n_objective: int,
        func: callable,
        jac: callable,
        hessian: callable,
        ref: Union[List[float], np.ndarray],
        mu: int = 5,
        h: Callable = None,
        h_jac: callable = None,
        h_hessian: callable = None,
        x0: np.ndarray = None,
        lower_bounds: Union[List[float], np.ndarray] = None,
        upper_bounds: Union[List[float], np.ndarray] = None,
        max_iters: Union[int, str] = np.inf,
        minimization: bool = True,
        xtol: float = 1e-3,
        HVtol: float = -np.inf,
        verbose: bool = True,
        **kwargs,
    ):
        """Hereafter, we use the following customized
        types to describe the usage:

        - Vector = List[float]
        - Matrix = List[Vector]

        Parameters
        ----------
        dim : int
            Dimensionality of the search space.
        obj_fun : Callable
            The objective function to be minimized.
        args: Tuple
            The extra parameters passed to function `obj_fun`.
        h : Callable, optional
            The equality constraint function, by default None.
        g : Callable, optional
            The inequality constraint function, by default None.
        x0 : Union[str, Vector, np.ndarray], optional
            The initial guess (by default None) which must fall between lower
            and upper bounds, if non-infinite values are provided for `lb` and
            `ub`. Note that, `x0` must be provided when `lb` and `ub` both
            take infinite values.
        sigma0 : Union[float], optional
            The initial step size, by default None
        C0 : Union[Matrix, np.ndarray], optional
            The initial covariance matrix which must be positive definite,
            by default None. Any non-positive definite input will be ignored.
        lb : Union[float, str, Vector, np.ndarray], optional
            The lower bound of search variables. When it is not a `float`,
            it must have the same length as `upper`, by default `-np.inf`.
        ub : Union[float, str, Vector, np.ndarray], optional
            The upper bound of search variables. When it is not a `float`,
            it must have the same length as `lower`, by default `np.inf`.
        ftarget : Union[int, float], optional
            The target value to hit, by default None.
        max_FEs : Union[int, str], optional
            Maximal number of function evaluations to make, by default `np.inf`.
        minimize : bool, optional
            To minimize or maximize, by default True.
        xtol : float, optional
            Absolute error in xopt between iterations that is acceptable for
            convergence, by default 1e-4.
        ftol : float, optional
            Absolute error in func(xopt) between iterations that is acceptable
            for convergence, by default 1e-4.
        n_restart : int, optional
            The maximal number of random restarts to perform when stagnation is
            detected during the run. The random restart can be switched off by
            setting `n_restart` to zero (the default value).
        verbose : bool, optional
            Verbosity of the output, by default False.
        logger : str, optional
            Name of the logger file, by default None, which turns off the
            logging behaviour.
        random_seed : int, optional
            The seed for pseudo-random number generators, by default None.
        """
        self.minimization = minimization
        self.dim_primal = dim
        self.n_objective = n_objective
        self.mu = mu  # the population size
        self.lower_bounds = lower_bounds
        self.upper_bounds = upper_bounds
        self.ref = ref
        # parameters controlling stop criteria
        self.xtol = xtol
        self.HVtol = HVtol
        self.stop_dict: Dict = {}
        # the objective function, gradient, and the Hessian
        self.func: Callable = func
        self.h: Callable = h
        self.h_jac: Callable = h_jac
        self.h_hessian: Callable = h_hessian
        self.hypervolume_derivatives = HypervolumeDerivatives(
            self.dim_primal, self.n_objective, ref, func, jac, hessian, minimization=minimization
        )
        self.iter_count: int = 0
        self.max_iters = max_iters
        self.verbose: bool = verbose
        self.eps = 1e-3 * np.max(self.upper_bounds - self.lower_bounds)
        self._init_logging_var()
        self._initialize(x0)

    def _initialize(self, X0: np.ndarray):
        if X0 is not None:
            X0 = np.asarray(X0)
            assert np.all(X0 - self.lower_bounds >= 0)
            assert np.all(X0 - self.upper_bounds <= 0)
            assert X0.shape[0] == self.mu
        else:
            # sample `x` u.a.r. in `[lb, ub]`
            assert all(~np.isinf(self.lower_bounds)) & all(~np.isinf(self.upper_bounds))
            X0 = (
                np.random.rand(self.mu, self.dim_primal) * (self.upper_bounds - self.lower_bounds)
                + self.lower_bounds
            )  # (mu, d)

        self._max_HV = np.product(self.ref)
        # initialize dual variables
        if self.h is not None:
            v = self.h(X0[0, :])
            self.n_eq_cstr = 1 if isinstance(v, float) else len(v)
            # to make the Hessian of Eq. constraints always a 3D tensor
            self._h_hessian = lambda x: self.h_hessian(x).reshape(self.n_eq_cstr, self.dim_primal, -1)
            X0 = np.c_[X0, np.ones((self.mu, self.n_eq_cstr)) / self.mu]
        else:
            self.n_eq_cstr = 0

        self._get_primal_dual = lambda X: (X[:, : self.dim_primal], X[:, self.dim_primal :])
        self.dim = self.dim_primal + self.n_eq_cstr
        self.X = X0
        self.Y = np.array([self.func(x) for x in self._get_primal_dual(self.X)[0]])  # (mu, n_objective)

    def _init_logging_var(self):
        """parameters for logging the history"""
        self.hist_Y: List[np.ndarray] = []
        self.hist_X: List[np.ndarray] = []
        self.hist_HV: List[float] = []
        self.hist_CPU_time_FE: List[int] = []
        self._delta_X: float = np.inf
        self._delta_Y: float = np.inf
        self._delta_HV: float = np.inf

        if self.h is not None:
            self.hist_G_norm: List[float] = []

        self.logger: logging.Logger = get_logger(
            logger_id=f"{self.__class__.__name__}",
            console=self.verbose,
        )

    @property
    def lower_bounds(self):
        return self._lower_bounds

    @lower_bounds.setter
    def lower_bounds(self, lb):
        self._lower_bounds = set_bounds(lb, self.dim_primal)

    @property
    def upper_bounds(self):
        return self._upper_bounds

    @upper_bounds.setter
    def upper_bounds(self, ub):
        self._upper_bounds = set_bounds(ub, self.dim_primal)

    @property
    def maxiter(self):
        return self._maxiter

    @maxiter.setter
    def maxiter(self, n: int):
        if n is None:
            self._maxiter = len(self.X) * 100

    def run(self) -> Tuple[np.ndarray, np.ndarray, Dict]:
        while not self.terminate():
            self.one_step()
            self.log()
        return self.X, self.Y, self.stop_dict

    def _precondition_hessian(self, H: np.ndarray) -> np.ndarray:
        """Precondition the Hessian matrix to make sure it is negative definite

        Args:
            H (np.ndarray): the Hessian matrix

        Returns:
            np.ndarray: the preconditioned Hessian
        """
        # pre-condition the Hessian
        beta = 1e-6
        v = np.min(np.diag(-H))
        tau = 0 if v > 0 else -v + beta
        I = np.eye(H.shape[0])
        for _ in range(35):
            try:
                cholesky(-H + tau * I, lower=True)
                break
            except:
                tau = max(1.5 * tau, beta)
        else:
            self.logger.warn("Pre-conditioning the HV Hessian failed")
        return H - tau * I

    def _compute_G(self, X: np.ndarray) -> np.ndarray:
        N = len(X)
        mud = int(N * self.dim_primal)
        primal_vars, dual_vars = self._get_primal_dual(X)
        out = self.hypervolume_derivatives.compute_gradient(primal_vars)
        self.FE_CPU_time += self.hypervolume_derivatives.FE_CPU_time
        HVdX = out["HVdX"].ravel()

        dH = block_diag(*[self.h_jac(x) for x in primal_vars])
        eq_cstr = np.array([self.h(_) for _ in primal_vars]).reshape(N, -1)
        G = np.concatenate([HVdX + dual_vars.ravel() @ dH, eq_cstr.ravel()])
        return np.c_[G[:mud].reshape(N, -1), G[mud:].reshape(N, -1)]

    def _compute_netwon_step(self, X: np.ndarray, Y: np.ndarray) -> Dict[str, np.ndarray]:
        N = X.shape[0]
        primal_vars, dual_vars = self._get_primal_dual(X)
        out = self.hypervolume_derivatives.compute_hessian(primal_vars, Y)
        self.FE_CPU_time += self.hypervolume_derivatives.FE_CPU_time

        HVdX, HVdX2 = out["HVdX"].ravel(), out["HVdX2"]
        # NOTE: preconditioning is needed EqDTLZ problems
        HVdX2 = self._precondition_hessian(HVdX2)
        H, G = HVdX2, HVdX

        if self.h is not None:  # with equality constraints
            mud = int(N * self.dim_primal)
            mup = int(N * self.n_eq_cstr)
            # record the CPU time of function evaluations
            t0 = time.process_time_ns()

            eq_cstr = np.array([self.h(_) for _ in primal_vars]).reshape(N, -1)  # (mu, p)
            dH = block_diag(*np.array([self.h_jac(x) for x in primal_vars]))  # (mu * p, mu * dim)
            ddH = block_diag(
                # NOTE: `np.einsum` is quite slow comparing the alternatives in np
                # TODO: ad-hoc solutions for now. Find a generci and faster solution later
                # *[np.einsum("ijk,i->jk", self._h_hessian(x), dual_vars[i]) for i, x in enumerate(primal_vars)]
                *[(self._h_hessian(x) * dual_vars[i])[0] for i, x in enumerate(primal_vars)]
            )  # (mu * dim, mu * dim)
            t1 = time.process_time_ns()

            G = np.concatenate([HVdX + dual_vars.ravel() @ dH, eq_cstr.ravel()])
            # NOTE: if the Hessian of the constraint is dropped, then quadratic convergence is gone
            H = np.concatenate(
                [
                    np.concatenate([HVdX2 + ddH, dH.T], axis=1),
                    np.concatenate([dH, np.zeros((mup, mup))], axis=1),
                ],
            )
        self.FE_CPU_time += t1 - t0
        try:
            # NOTE: use the sparse matrix representation to save some time here
            step = -1 * spsolve(csc_matrix(H), csc_matrix(G.reshape(-1, 1)))
        except:
            # NOTE: this part should not occur
            w, V = np.linalg.eigh(H)
            w[np.isclose(w, 0)] = 1e-6
            D = np.diag(1 / w)
            step = -1 * V @ D @ V.T @ G

        if self.h is not None:
            step = np.c_[step[:mud].reshape(N, -1), step[mud:].reshape(N, -1)]
            G = np.c_[G[:mud].reshape(N, -1), G[mud:].reshape(N, -1)]
            return dict(step=step, G=G)
        else:
            return dict(step=step.reshape(N, -1), G=HVdX.reshape(N, -1))

    def one_step(self):
        self.FE_CPU_time = 0  # clear the CPU time counter
        self._check_XY()
        self.step = np.zeros((self.mu, self.dim))
        self.step_size = np.ones(self.mu)
        self.G = np.zeros((self.mu, self.dim))

        # partition the approximation set to by feasibility
        self._nondominated_idx = non_domin_sort(self.Y, only_front_indices=True)[0]
        if self.h is None:
            feasible_mask = np.array([True] * self.mu)
        else:
            eq_cstr = np.array([self.h(_) for _ in self._get_primal_dual(self.X)[0]]).reshape(self.mu, -1)
            feasible_mask = np.all(np.isclose(eq_cstr, 0, atol=1e-4, rtol=0), axis=1)

        feasible_idx = np.nonzero(feasible_mask)[0]
        dominated_idx = list((set(range(self.mu)) - set(self._nondominated_idx) - set(feasible_idx)))
        if np.any(feasible_mask):
            # non-dominatd sorting of the feasible points
            partitions = non_domin_sort(self.Y[feasible_mask], only_front_indices=True)
            partitions = {k: feasible_idx[v] for k, v in partitions.items()}
            partitions.update({0: np.sort(np.r_[partitions[0], np.nonzero(~feasible_mask)[0]])})
        else:
            partitions = {0: np.array(range(self.mu))}

        # compute the Newton direction for each partition
        for _, idx in partitions.items():
            out = self._compute_netwon_step(X=self.X[idx], Y=self.Y[idx])
            self.step[idx, :] = out["step"]
            self.G[idx, :] = out["G"]
            # backtracking line search with Armijo's condition for each point
            if _ == 0 and len(dominated_idx) > 0:
                idx_ = list(set(idx) - set(dominated_idx))
                for k in dominated_idx:
                    self.step_size[k] = self._linear_search2(self.X[[k]], self.step[[k]])
                self.step_size[idx_] = self._linear_search(self.X[idx_], self.step[idx_], G=self.G[idx_])
            else:
                self.step_size[idx] = self._linear_search(self.X[idx], self.step[idx], G=self.G[idx])

        self.X += self.step_size.reshape(-1, 1) * self.step
        # evaluation
        self.Y = np.array([self.func(x) for x in self._get_primal_dual(self.X)[0]])
        self.iter_count += 1

    def _linear_search(self, X: np.ndarray, step: np.ndarray, G: np.ndarray) -> float:
        """backtracking line search with Armijo's condition"""
        c = 1e-5
        N = len(X)
        primal_vars = self._get_primal_dual(X)[0]
        normal_vectors = np.c_[np.eye(self.dim_primal * N), -1 * np.eye(self.dim_primal * N)]
        # calculate the maximal step-size
        dist = np.r_[
            np.abs(primal_vars.ravel() - np.tile(self.lower_bounds, N)),
            np.abs(np.tile(self.upper_bounds, N) - primal_vars.ravel()),
        ]
        v = step[:, : self.dim_primal].ravel() @ normal_vectors
        alpha = min(1, 0.25 * np.min(dist[v < 0] / np.abs(v[v < 0])))

        for _ in range(6):
            X_ = X + alpha * step
            if self.h is None:
                HV = self.hypervolume_derivatives.HV(X)
                HV_ = self.hypervolume_derivatives.HV(X_)
                inc = np.inner(G.ravel(), step.ravel())
                cond = HV_ - HV >= c * alpha * inc
            else:
                G_ = self._compute_G(X_)
                cond = np.linalg.norm(G_) <= (1 - c * alpha) * np.linalg.norm(G)
            if cond:
                break
            else:
                if 11 < 2:
                    phi0 = HV if self.h is None else np.sum(G**2) / 2
                    phi1 = HV_ if self.h is None else np.sum(G_**2) / 2
                    phi0prime = inc if self.h is None else -np.sum(G**2)
                    alpha = -phi0prime * alpha**2 / (phi1 - phi0 - phi0prime * alpha) / 2
                    # alpha *= tau
                if 1 < 2:
                    alpha *= 0.5
        else:
            self.logger.warn("Armijo's backtracking line search failed")
        return alpha

    def _linear_search2(self, X: np.ndarray, step: np.ndarray) -> float:
        """backtracking line search with Armijo's condition"""
        c = 1e-4
        N = len(X)
        step = step[:, : self.dim_primal]
        primal_vars = self._get_primal_dual(X)[0]
        normal_vectors = np.c_[np.eye(self.dim_primal * N), -1 * np.eye(self.dim_primal * N)]
        # calculate the maximal step-size
        dist = np.r_[
            np.abs(primal_vars.ravel() - np.tile(self.lower_bounds, N)),
            np.abs(np.tile(self.upper_bounds, N) - primal_vars.ravel()),
        ]
        v = step.ravel() @ normal_vectors
        alpha = min(1, 0.25 * np.min(dist[v < 0] / np.abs(v[v < 0])))

        h_ = self.h(primal_vars)
        eq_cstr = h_**2 / 2
        G = h_ * self.h_jac(primal_vars)
        for _ in range(6):
            X_ = primal_vars + alpha * step
            eq_cstr_ = self.h(X_) ** 2 / 2
            dec = np.inner(G.ravel(), step.ravel())
            cond = eq_cstr_ - eq_cstr <= c * alpha * dec
            if cond:
                break
            else:
                alpha *= 0.5
        else:
            self.logger.warn("Armijo's backtracking line search failed")
        return alpha

    def _check_XY(self):
        # get unique points: if some points converge to the same location
        primal_vars = self.X[:, : self.dim_primal]
        D = cdist(primal_vars, primal_vars)
        drop_idx_X = set([])
        for i in range(self.mu):
            if i not in drop_idx_X:
                drop_idx_X |= set(np.nonzero(D[i, :] < self.eps)[0]) - set([i])

        # get rid of weakly-dominated points
        drop_idx_Y = set([])
        for i in range(self.mu):
            if i not in drop_idx_Y:
                drop_idx_Y |= set(np.nonzero(np.isclose(self.Y[i, :], self.Y))[0]) - set([i])

        idx = list(set(range(self.mu)) - (drop_idx_X | drop_idx_Y))
        self.mu = len(idx)
        self.X = self.X[idx, :]
        self.Y = self.Y[idx, :]

    def log(self):
        HV = hypervolume(self.Y, self.ref)
        self.hist_Y += [self.Y.copy()]
        self.hist_X += [self._get_primal_dual(self.X.copy())[0]]
        self.hist_HV += [HV]
        self.hist_CPU_time_FE += [self.FE_CPU_time]

        if self.verbose:
            self.logger.info(f"iteration {self.iter_count} ---")
            self.logger.info(f"HV: {HV}")
            # self.logger.info(f"step size: {self.step_size}")
            self.logger.info(f"CPU time of FEs: {self.FE_CPU_time}")

        if self.iter_count >= 1:
            try:
                self._delta_X = np.mean(np.sqrt(np.sum((self.hist_X[-1] - self.hist_X[-2]) ** 2, axis=1)))
                self._delta_Y = np.mean(np.sqrt(np.sum((self.hist_Y[-1] - self.hist_Y[-2]) ** 2, axis=1)))
                self._delta_HV = np.abs(self.hist_HV[-1] - self.hist_HV[-2])
            except:
                pass

        if self.h is not None:
            self.hist_G_norm += [np.median(np.linalg.norm(self.G[self._nondominated_idx], axis=1))]
            self.logger.info(f"G norm: {self.hist_G_norm[-1]}")

    def terminate(self) -> bool:
        if self.iter_count >= self.max_iters:
            self.stop_dict["iter_count"] = self.iter_count

        # if self._delta_HV < self.HVtol:
        #     self.stop_dict["HVtol"] = self._delta_HV
        #     self.stop_dict["iter_count"] = self.iter_count

        # if self._delta_X < self.xtol:
        #     self.stop_dict["xtol"] = self._delta_X
        #     self.stop_dict["iter_count"] = self.iter_count

        return bool(self.stop_dict)
