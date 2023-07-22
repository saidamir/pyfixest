import pyhdfe
import numpy as np
import pandas as pd
import warnings

from typing import Union, List, Dict

from pyfixest.feols import Feols, _check_vcov_input, _deparse_vcov_input
from pyfixest.ssc_utils import get_ssc
from pyfixest.exceptions import VcovTypeNotSupportedError, NanInClusterVarError, NonConvergenceError, NotImplementedError

class Fepois(Feols):

    '''
    Class to estimate Poisson Regressions. Inherits from Feols. The following methods are overwritten: `get_fit()`.
    '''

    def __init__(self, Y, X, fe, weights, drop_singletons, maxiter = 25, tol = 1e-08):

        '''
        Args:
            Y (np.array): dependent variable
            Z (np.array): independent variables
            fe (np.array): fixed effects
            weights (np.array): weights
            drop_singletons (bool): whether to drop singleton fixed effects
            maxiter (int): maximum number of iterations for the IRLS algorithm
            tol (float): tolerance level for the convergence of the IRLS algorithm
        '''

        super().__init__(Y = Y, X = X, Z = X, weights = weights)

        self.fe = fe
        self.maxiter = maxiter
        self.tol = tol
        self.drop_singletons = drop_singletons


    def get_fit(self) -> None:

        '''
        Fit a Poisson Regression Model via Iterated Weighted Least Squares

        Args:
            tol (float): tolerance level for the convergence of the IRLS algorithm
            maxiter (int): maximum number of iterations for the IRLS algorithm. 25 by default.
        Returns:
            None
        Attributes:
            beta_hat (np.array): estimated coefficients
            Y_hat (np.array): predicted values of the dependent variable
            u_hat (np.array): estimated residuals
            tZX (np.array): transpose of the product of the demeaned Z and X matrices (used for vcov calculation)
            tZy (np.array): transpose of the product of the demeaned Z and Y matrices (used for vcov calculation)
            tZXinv (np.array): inverse of tZX (used for vcov calculation)
        Updates the following attributes:
            X (np.array): demeaned X matrix from the last iteration of the IRLS algorithm (X_d) x weights
            Z (np.array): demeaned X matrix from the last iteration of the IRLS algorithm (X_d) x weights
            Y (np.array): demeaned Y matrix from the last iteration of the IRLS algorithm (Z_d) x weights
        '''

        Y = self.Y
        X = self.X
        fe = self.fe


        def _update_w(Xbeta):

            '''
            Implements the updating function for the weights in the IRLS algorithm.
            Uses the softmax for numerical stability.
            Args:
                Xbeta (np.array): Xbeta from the last iteration of the IRLS algorithm
            Returns:
                w (np.array): updated weights
            '''
            expXbeta = np.exp(Xbeta - np.max(Xbeta))
            return expXbeta / np.sum(expXbeta)

        def _update_Z(Y, Xbeta):
            return (Y - np.exp(Xbeta)) / np.exp(Xbeta) + Xbeta

        # starting values: http://sfb649.wiwi.hu-berlin.de/fedc_homepage/xplore/ebooks/html/spm/spmhtmlnode27.html
        # reference:  McCullagh, P. & Nelder, J. A. ( 1989). Generalized Linear Models,
        #  Vol. 37 of Monographs on Statistics and Applied Probability, 2 edn, Chapman and Hall, London.
        Xbeta = np.log(np.repeat(np.mean(Y), self.N).reshape((self.N, 1)))
        w = _update_w(Xbeta)
        Z = _update_Z(Y = Y, Xbeta = Xbeta)

        delta = np.ones((X.shape[1], 1))

        X2 = X.copy()
        Z2 = Z

        for x in range(self.maxiter):

            # Step 1: weighted demeaning
            ZX = np.concatenate([Z2, X2], axis = 1)

            if fe is not None:
                algorithm = pyhdfe.create(
                        ids=fe,
                        residualize_method='map',
                        drop_singletons=self.drop_singletons,
                        weights = w
                )
                if self.drop_singletons == True and algorithm.singletons != 0 and algorithm.singletons is not None:
                    print(algorithm.singletons, "columns are dropped due to singleton fixed effects.")
                    dropped_singleton_indices = np.where(algorithm._singleton_indices)[0].tolist()
                    na_index += dropped_singleton_indices
                ZX_d = algorithm.residualize(ZX)
            else:
                ZX_d = ZX

            Z_d = ZX_d[:,0].reshape((self.N, 1))
            X_d = ZX_d[:,1:]

            WX_d = np.sqrt(w) * X_d
            WZ_d = np.sqrt(w) * Z_d

            XdWXd = WX_d.transpose() @ WX_d
            XdWZd = WX_d.transpose() @ WZ_d

            delta_new = np.linalg.solve(XdWXd, XdWZd)
            e_new = Z_d - X_d.transpose().reshape((self.N, self.k)) @ delta_new

            Xbeta_new = Z - e_new
            w_u = _update_w(Xbeta_new)
            Z_u = _update_Z(Y = Y, Xbeta = Xbeta_new)

            stop_iterating = np.sqrt(np.sum((delta - delta_new) ** 2) / np.sum(delta ** 2)) < self.tol

            # update
            delta = delta_new
            Z2 = Z_d + Z_u - Z
            X2 = X_d
            Z = Z_u
            w_old = w.copy()
            w = w_u
            Xbeta = Xbeta_new

            if stop_iterating:
                break
            if x == self.maxiter:
                raise NonConvergenceError("The IRLS algorithm did not converge. Try to increase the maximum number of iterations.")

        self.beta_hat = delta.flatten()
        self.Y_hat = np.exp(Xbeta)
        self.u_hat = e_new #(Y - self.Y_hat)
        # needed for the calculation of the vcov

        # updat for inference
        self.weights = w_old
        # if only one dim
        if self.weights.ndim == 1:
            self.weights = self.weights.reshape((self.N, 1))

        self.X = X_d
        self.Z = X_d
        #self.Y = np.sqrt(weights) * Z_d

        self.tZX = np.transpose(self.Z) @ self.X
        #self.tZy = (np.transpose(self.Z) @ self.Y)
        self.tZXinv = np.linalg.inv(self.tZX)
        self.Xbeta = Xbeta


    def get_vcov(self, vcov: Union[str, Dict[str, str], List[str]]) -> None:
        '''
        Compute covariance matrices for an estimated regression model.

        Parameters
        ----------
        vcov : Union[str, Dict[str, str], List[str]]
            A string or dictionary specifying the type of variance-covariance matrix to use for inference.
            If a string, can be one of "iid", "hetero", "HC1", "HC2", "HC3".
            If a dictionary, it should have the format {"CRV1":"clustervar"} for CRV1 inference
            or {"CRV3":"clustervar"} for CRV3 inference.
            Note that CRV3 inference is currently not supported with arbitrary fixed effects and IV estimation.

        Raises
        ------
        AssertionError
            If vcov is not a dict, string, or list.
        AssertionError
            If vcov is a dict and the key is not "CRV1" or "CRV3".
        AssertionError
            If vcov is a dict and the value is not a string.
        AssertionError
            If vcov is a dict and the value is not a column in the data.
        AssertionError
            CRV3 currently not supported with arbitrary fixed effects
        AssertionError
            If vcov is a list and it does not contain strings.
        AssertionError
            If vcov is a list and it does not contain columns in the data.
        AssertionError
            If vcov is a string and it is not one of "iid", "hetero", "HC1", "HC2", or "HC3".


        Returns
        -------
        None

        '''

        _check_vcov_input(vcov, self.data)

        self.vcov_type, self.vcov_type_detail, self.is_clustered, self.clustervar = _deparse_vcov_input(vcov, self.has_fixef, self.is_iv)

        # compute vcov
        W = np.diag(self.weights.flatten())
        bread = np.linalg.inv(self.X.transpose() @ W @ self.X)

        if self.vcov_type == 'iid':

            raise NotImplementedError("iid inference is not supported for non-linear models.")

            self.ssc = get_ssc(
                ssc_dict = self.ssc_dict,
                N = self.N,
                k = self.k,
                G = 1,
                vcov_sign = 1,
                vcov_type='iid'
            )

            #self.vcov = self.ssc

            # only relevant factor for iid in ssc: fixef.K
            WX = self.weights * self.X
            self.vcov =  self.ssc * WX * np.sum( (self.weights * self.u_hat) ** 2) / (self.N - 1)


        elif self.vcov_type == 'hetero':

            if self.vcov_type_detail in ["HC2", "HC3"]:
                raise NotImplementedError("HC2 and HC3 are not implemented for non-linear models.")

            self.ssc = get_ssc(
                ssc_dict = self.ssc_dict,
                N = self.N,
                k = self.k,
                G = 1,
                vcov_sign = 1,
                vcov_type='hetero'
            )


            Sigma = np.diag(self.u_hat.flatten() ** 2)
            meat = self.X.transpose() @ W @ Sigma @ W @ self.X

            self.vcov = self.ssc * bread @ meat @ bread

        elif self.vcov_type == "CRV":

            cluster_df = self.data[self.clustervar]
            # if there are missings - delete them!

            if cluster_df.dtype != "category":
                cluster_df = pd.Categorical(cluster_df)

            if cluster_df.isna().any():
                raise NanInClusterVarError(
                    "CRV inference not supported with missing values in the cluster variable."
                    "Please drop missing values before running the regression."
                )

            _, clustid = pd.factorize(cluster_df)

            self.G = len(clustid)

            self.ssc = get_ssc(
                ssc_dict = self.ssc_dict,
                N = self.N,
                k = self.k,
                G = self.G,
                vcov_sign = 1,
                vcov_type = "CRV"
            )

            if self.vcov_type_detail == "CRV1":

                k = self.X.shape[1]
                meat = np.zeros((k, k))

                for g in range(self.G):
                    X_g = self.X[np.where(cluster_df == g)]
                    u_g = self.u_hat[np.where(cluster_df == g)]
                    W_g = np.diag(self.weights.flatten()[np.where(cluster_df == g)])
                    meat_g = X_g.transpose() @ W_g @ u_g @ u_g.transpose() @ W_g @ X_g
                    meat += meat_g

                self.vcov = self.ssc * bread @ meat @ bread

            else:

                raise NotImplementedError(
                    "CRV3 inference is not supported for non-linear models."
                )





