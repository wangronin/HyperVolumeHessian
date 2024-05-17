from functools import cached_property
from typing import Callable, Dict, List, Optional, Tuple, Union

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist, directed_hausdorff
from sklearn_extra.cluster import KMedoids

__authors__ = ["Hao Wang"]


# TODO: think of a better abstraction here.
class ReferenceSet:
    def __init__(self, ref: np.ndarray, p: float = 2, Y_idx: Optional[np.ndarray] = None) -> None:
        self._ref: Dict[int, np.ndarray] = {0: ref} if isinstance(ref, np.ndarray) else ref
        assert isinstance(self._ref, dict)
        self.n_components: int = len(self._ref)  # the number of connected components of the reference set
        self.p: float = p
        self.dim: int = self._ref[0].shape[1]
        self.N: int = len(self.reference_set)
        self.Y_idx: List[np.ndarray] = Y_idx  # list of indices of clusters of the approximation set
        self._medoids: Dict[int, np.ndarray] = {i: None for i in range(self.n_components)}
        self.re_match: bool = True

    @cached_property
    def reference_set(self) -> np.ndarray:
        return np.concatenate(list(self._ref.values()), axis=0)

    @property
    def medoids(self) -> np.ndarray:
        return self._matched_medoids

    def match(self, Y: np.ndarray) -> np.ndarray:
        if not self.re_match and hasattr(self, "_matched_medoids"):
            return

        Y = self._partition_Y(Y)
        N = sum([len(y) for y in Y])
        # match the components of the reference set and `Y`
        idx = self._match_components(Y)
        out = np.zeros((N, self.dim))
        self._medoids_idx = np.empty(N, dtype=object)
        for k, v in enumerate(idx):
            Y_ = Y[v]
            assert len(self._ref[k]) >= len(Y_)
            try:
                medoids = self._medoids[k]
                assert len(medoids) == len(Y_)
            except:
                medoids = self._cluster(component=self._ref[k], k=len(Y_))
            medoids = self._match(medoids, Y_)
            self._medoids[k] = medoids
            out[self.Y_idx[v]] = medoids
            for i, j in enumerate(self.Y_idx[v]):
                self._medoids_idx[j] = (k, i)
        self._matched_medoids = out

    def shift(
        self,
        eta=None,
    ):
        # TODO: also shift the reference set here
        self._medoids[k] += v
        self.ref.set_medoids(self._medoids[k], k)

    def set_medoids(self, medroid: np.ndarray, k: int):
        # update the medoids
        c, idx = self._medoids_idx[k]
        self._medoids[c][idx] = medroid

    def _partition_Y(self, Y: np.ndarray) -> List[np.ndarray]:
        """partition the approximation set `Y`. We consider the following scenarios:
        - If the reference set is not clustered, then `Y` should not be partitioned.
           In this case, `self.Y_idx` is ignored.
        - If the reference set is clustered (`self.n_components` indicates the number), then
            - we take the partitioning of `Y` indicated by `self.Y_idx` if it is not `None`;
            - otherwise, we assign each element of `Y` to the closest cluster of `self._ref`

        Args:
            Y (np.ndarray): the approximation set of shape (N, `self.dim`)

        Returns:
            List[np.ndarray]: a list of partitions of `Y`
        """
        if self.n_components == 1:
            self.Y_idx = [list(range(len(Y)))]
            Y_partitions = [Y]
        else:
            if self.Y_idx is None:
                D = np.array([cdist(self._ref[i], Y).min(axis=1) for i in range(self.n_components)])
                labels = np.argmin(D, axis=1)
                self.Y_idx = [np.nonzero(labels == i)[0] for i in range(self.n_components)]
            # parition `Y` according to `Y_idx`
            Y_partitions = [Y[idx] for idx in self.Y_idx]
        return Y_partitions

    def _match_components(self, Y: Dict[int, np.ndarray]) -> np.ndarray:
        n, m = self.n_components, len(Y)
        # assert n >= m  # TODO: find a solution here
        if n == 1 and m == 1:
            idx = [0]
        else:
            # match the clusters of `X` and `Y`
            idx = np.zeros(m)
            cost = np.array([directed_hausdorff(self._ref[i], Y[j])[0] for i in range(n) for j in range(m)])
            cost = cost.reshape(n, m)
            idx = linear_sum_assignment(cost)[1]
        return idx

    def _cluster(self, component: np.ndarray, k: int) -> np.ndarray:
        """cluster the reference components with k-medoids method
        Args:
            component (np.ndarray): a component of the reference set
            K (int): the number of medoids

        Returns:
            np.ndarray: _description_
        """
        # TODO: "pam" method in Python is really slow
        # method = "pam" if len(X) <= 3000 else "alternate"
        method = "alternate"
        km = KMedoids(n_clusters=k, method=method, random_state=0, init="k-medoids++").fit(component)
        return component[km.medoid_indices_]

    def _match(self, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
        cost = cdist(Y, X)
        idx = linear_sum_assignment(cost)[1]  # min-weight assignment in a bipartite graph
        return X[idx]


class GenerationalDistance:
    def __init__(
        self,
        ref: ReferenceSet,
        func: Callable = None,
        jac: Callable = None,
        hess: Callable = None,
        p: float = 2,
    ):
        """Generational Distance

        Args:
            ref (np.ndarray): the reference set, of shape (n_point, n_objective)
            func (Callable): the objective function, which returns an array of shape (n_objective, )
            jac (Callable): the Jacobian function, which returns an array of shape (n_objective, dim)
            hess (Callable): the Hessian function, which returns an array of shape (n_objective, dim, dim)
            p (float, optional): parameter in the p-norm. Defaults to 2.
        """
        self._ref = ref
        self.p = p
        self.func = func
        self.jac = jac
        self.hess = hess

    def __str__(self) -> str:
        return "GD"

    def _compute_indices(self, Y: np.ndarray):
        # find for each approximation point, the index of its closest point in the reference set
        self.D = cdist(Y, self._ref.reference_set)
        self.indices = np.argmin(self.D, axis=1)

    def compute(self, X: np.ndarray = None, Y: np.ndarray = None) -> float:
        """compute the generational distance value

        Args:
            X (np.ndarray, optional): the decision points of shape (N, dim). Defaults to None.
            Y (np.ndarray, optional): the objective points of shape (N, n_objective). Defaults to None.
                `X` and `Y` cannot be None at the same time

        Returns:
            float: the indicator value
        """
        if Y is None:
            assert X is not None
            Y = np.array([self.func(x) for x in X])
        self._compute_indices(Y)
        return np.mean(self.D[np.arange(len(Y)), self.indices] ** self.p) ** (1 / self.p)

    def compute_derivatives(
        self,
        X: np.ndarray,
        Y: np.ndarray = None,
        compute_hessian: bool = True,
        Jacobian: np.ndarray = None,
    ) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
        """compute the derivatives of the generational distance^p

        Args:
            X (np.ndarray): the decision points of shape (N, dim).
            Y (np.ndarray, optional): the objective points of shape (N, n_objective). Defaults to None.
            compute_hessian (bool, optional): whether the Hessian is computed. Defaults to True.

        Returns:
            Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
                if `compute_hessian` = True, it returns (gradient, Hessian)
                otherwise, it returns (gradient, )
        """
        N, dim = X.shape
        c1 = self.p / N

        if Y is None:
            Y = np.array([self.func(x) for x in X])

        self._compute_indices(Y)
        J = np.array([self.jac(x) for x in X]) if Jacobian is None else Jacobian  # (N, n_objective, dim)
        diff = Y - self._ref.reference_set[self.indices]  # (N, n_objective)
        diff_norm = np.sqrt(np.sum(diff**2, axis=1)).reshape(-1, 1)  # (N, 1)
        grad_ = np.einsum("ijk,ij->ik", J, diff)  # (N, dim)
        grad = c1 * diff_norm ** (self.p - 2) * grad_  # (N, dim)

        if compute_hessian:
            c2 = self.p * (self.p - 2) / N
            H = np.array([self.hess(x) for x in X])  # (N, n_objective, dim, dim)
            idx = diff_norm != 0
            # some `diff` can be zero
            diff_norm_ = np.zeros(diff_norm.shape)
            diff_norm_[idx] = diff_norm[idx] ** (self.p - 4)
            diff_norm_ = diff_norm_[..., np.newaxis]
            # TODO: test this part for p != 2
            term = (
                c2 * np.tile(diff_norm_, (1, dim, dim)) * np.einsum("ij,ik->ijk", grad_, grad_)
                if self.p != 2
                else 0
            )
            hessian = term + c1 * (
                np.einsum("ijk,ijl->ikl", J, J) + np.einsum("ijkl,ij->ikl", H, diff)
            )  # (N, dim, dim)
            return grad, hessian
        else:
            return grad


class InvertedGenerationalDistance:
    def __init__(
        self,
        ref: ReferenceSet,
        func: Callable = None,
        jac: Callable = None,
        hess: Callable = None,
        p: float = 2,
        cluster_matching: bool = False,
    ):
        """Generational Distance

        Args:
            ref (np.ndarray): the reference set, of shape (n_point, n_objective)
            func (Callable): the objective function, which returns an array of shape (n_objective, )
            jac (Callable): the Jacobian function, which returns an array of shape (n_objective, dim)
            hess (Callable): the Hessian function, which returns an array of shape (n_objective, dim, dim)
            p (float, optional): parameter in the p-norm. Defaults to 2.
        """
        self.ref = ReferenceSet(ref, p)
        self.p = p
        self.func = func
        self.jac = jac
        self.hess = hess
        self.M = self.ref.N
        self.cluster_matching = cluster_matching

    def __str__(self) -> str:
        return "IGD (w/matching)" if self.cluster_matching else "IGD"

    def _compute_indices(self, Y: np.ndarray):
        """find for each reference point, the index of its closest point in the approximation set

        Args:
            Y (np.ndarray): the objective points of shape (N, n_objective).
        """
        N = len(Y)
        self.D = cdist(self.ref.reference_set, Y)
        # for each reference point, the index of its closest point in the approximation set `Y`
        self._indices = np.argmin(self.D, axis=1)
        # for each point `p`` in `Y`, the indices of points in the reference set
        # which have `p` as the closest point.
        self.indices = [np.nonzero(self._indices == i)[0] for i in range(N)]
        # for each point `p` in `Y`, the total number of points in the reference set
        # which have `p` as the closest point
        self.m = np.zeros((N, 1))
        idx, counts = np.unique(self._indices, return_counts=True)
        self.m[idx, 0] = counts

    def compute(self, X: np.ndarray = None, Y: np.ndarray = None) -> float:
        """compute the inverted generational distance value

        Args:
            X (np.ndarray, optional): the decision points of shape (N, dim). Defaults to None.
            Y (np.ndarray, optional): the objective points of shape (N, n_objective). Defaults to None.
                `X` and `Y` cannot be None at the same time

        Returns:
            float: the indicator value
        """
        if Y is None:
            assert X is not None
            assert self.func is not None
            Y = np.array([self.func(x) for x in X])
        if self.cluster_matching:
            self.ref.match(Y)
            return np.mean(np.sum((Y - self.ref.medoids) ** 2, axis=1))
        else:
            self._compute_indices(Y)
            return np.mean(self.D[np.arange(self.M), self._indices] ** self.p) ** (1 / self.p)

    def compute_derivatives(
        self,
        X: np.ndarray,
        Y: np.ndarray = None,
        compute_hessian: bool = True,
        Jacobian: np.ndarray = None,
    ) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
        """compute the derivatives of the inverted generational distance^p

        Args:
            X (np.ndarray): the decision points of shape (N, dim).
            Y (np.ndarray, optional): the objective points of shape (N, n_objective). Defaults to None.
            compute_hessian (bool, optional): whether the Hessian is computed. Defaults to True.

        Returns:
            Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
                if `compute_hessian` = True, it returns (gradient, Hessian)
                otherwise, it returns (gradient, )
        """
        assert self.jac is not None
        assert self.hess is not None
        # TODO: implement p != 2
        # NOTE: for the matching method, `self.M = 1` since it is one-to-one matching
        c = 2 if self.cluster_matching else 2 / self.M
        dim = X.shape[1]
        if Y is None:
            Y = np.array([self.func(x) for x in X])
        # Jacobian of the objective function
        if Jacobian is None:
            J = np.array([self.jac(x) for x in X])  # (N, n_obj, dim)
        else:
            J = Jacobian
        if self.cluster_matching:
            self.ref.match(Y)
            diff = Y - self.ref.medoids  # (N, n_obj)
        else:
            self._compute_indices(Y)
            Z = np.array([np.sum(self.ref.reference_set[idx], axis=0) for idx in self.indices])  # (N, n_obj)
            diff = self.m * Y - Z

        grad = c * np.einsum("ijk,ij->ik", J, diff)  # (N, dim)
        if compute_hessian:
            H = np.array([self.hess(x) for x in X])  # (N, n_obj, dim, dim)
            m = np.tile(self.m[..., np.newaxis], (1, dim, dim)) if not self.cluster_matching else 1
            hessian = c * (
                m * np.einsum("ijk,ijl->ikl", J, J) + np.einsum("ijkl,ij->ikl", H, diff)
            )  # (N, dim, dim)
            return grad, hessian
        else:
            return grad
