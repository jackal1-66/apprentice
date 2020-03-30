import numpy as np
from apprentice import tools
from scipy.optimize import minimize
from timeit import default_timer as timer
import apprentice

from numba import jit, njit

def fast_robustSampleV(coeff, q_ipo, M, N):
    return np.sum(coeff[M:M+N] * q_ipo, axis=1) - 1.0

def fast_leastSqObj(coeff, trainingsize, ipop, ipoq, M, N, Y):
    return np.sum(np.square(Y * np.sum(coeff[M:M+N] * ipoq, axis=1) - np.sum(coeff[:M] * ipop, axis=1)))

def fast_jac(coeff, _, ipop, ipoq, M, N, Y):
    """
    Exact analytic gradient of fast_leastSqObj
    """
    core = Y * np.sum(coeff[M:M+N] * ipoq, axis=1) - np.sum(coeff[:M] * ipop, axis=1)
    grads = np.empty_like(coeff)

    pgrad = -2*np.sum(core[:,np.newaxis]                    *ipop, axis=0)
    qgrad =  2*np.sum(core[:,np.newaxis] * Y[:,np.newaxis] * ipoq, axis=0)

    grads[:M]    = pgrad
    grads[M:M+N] = qgrad

    return grads

class RationalApproximationSLSQP(apprentice.RationalApproximation):
    def __init__(self, *args, **kwargs):
        """
        Multivariate rational approximation p(x)_m/q(x)_n

        args:
            X       --- anchor points
            Y       --- function values

        kwargs:
            order (tuple m,n)
                            --- order of the numerator polynomial --- if omitted: auto 1 used
                            --- order of the denominator polynomial --- if omitted: auto 1 used
            pnames          --- list of parameter names to pass to the scaler to scale
        """
        self._vmin=None
        self._vmax=None
        self._debug = kwargs["debug"] if kwargs.get("debug") is not None else  False

        self._m=kwargs["order"][0]
        self._n=kwargs["order"][1]
        import os
        if len(args) == 0:
            pass
        else:
            # Scaler related kwargs
            _pnames      = kwargs["pnames"]   if kwargs.get("pnames")   is not None else None
            _scale_min   = kwargs["scalemin"] if kwargs.get("scalemin") is not None else -1
            _scale_max   = kwargs["scalemax"] if kwargs.get("scalemax") is not None else  1
            self._scaler = apprentice.Scaler(np.array(args[0], dtype=np.float64), a=_scale_min, b=_scale_max, pnames=_pnames)
            self._X      = self._scaler.scaledPoints
            self._trainingsize = len(self._X)
            self._dim = self._X[0].shape[0]
            self._Y      = np.array(args[1], dtype=np.float64)
            if self._dim==1: self.recurrence=apprentice.monomial.recurrence1D
            else           : self.recurrence=apprentice.monomial.recurrence
            self.setStructures()
            self.setIPO()
            self.fit()

    @property
    def trainingsize(self): return self._trainingsize
    @property
    def box(self): return self._scaler.box_scaled

    def setIPO(self):
        """
        Calculate the Pade approximation
        """
        self._ipo            = np.empty((self.trainingsize,2), "object")
        for i in range(self.trainingsize):
            self._ipo[i][0] = self.recurrence(self._X[i,:],self._struct_p)
            self._ipo[i][1] = self.recurrence(self._X[i,:],self._struct_q)

    def scipyfit(self, coeffs0, cons, maxiter=1001, ftol=1e-9, iprint=2):
        start = timer()
        ipop = np.array([self._ipo[i][0] for i in range(self.trainingsize)])
        ipoq = np.array([self._ipo[i][1] for i in range(self.trainingsize)])
        ret = minimize(fast_leastSqObj, coeffs0 , args=(self.trainingsize, ipop, ipoq, self.M, self.N, self._Y),
                jac=fast_jac, method = 'SLSQP', constraints=cons,
                options={'maxiter': maxiter, 'ftol': ftol, 'disp': self._debug, 'iprint': iprint})
        end = timer()
        optstatus = {'message':ret.get('message'),'status':ret.get('status'),'noOfIterations':ret.get('nit'),'time':end-start}
        return ret.get('x'), ret.get('fun'), optstatus

    def fit(self, maxIterations=1000, maxRestarts=100, threshold=0.02):

        cons = np.empty(0, "object")
        coeffs0 = []

        ipoq = np.array([self._ipo[i][1] for i in range(self.trainingsize)])
        cons = np.append(cons, {'type': 'ineq', 'fun':fast_robustSampleV, 'args':(ipoq, self.M, self.N)})

        # TODO need to check if this is a feasible point!
        coeffs0 = np.ones((self.M+self.N))
        coeffs0[self.M] = 2

        self._iterationinfo = []
        for iter in range(1, maxIterations+1):
            data = {}
            coeffs, leastSq, optstatus = self.scipyfit(coeffs0, cons)
            # This is a bit brutal trial and error,
            # if the starting point was not good, we just try again with a random
            # vector, otherwise coeffs is always the same and this loop does nothing
            # but waste time
            if optstatus['status']!=0:
                fixme=True
                while fixme:
                    coeffs0 = np.random.random(coeffs0.shape)
                    coeffs, leastSq, optstatus = self.scipyfit(coeffs0, cons)
                    if optstatus['status']!=0:
                        fixme=False

            data['pcoeff'] = coeffs[0:self.M].tolist()
            data['qcoeff'] = coeffs[self.M:self.M+self.N].tolist()

            robO = 0
            x, robO, restartInfo = self.multipleRestartForIterRobO(coeffs,maxRestarts,threshold)
            data['robOptInfo'] = {'robustArg':x.tolist(),'robustObj':robO,'info':restartInfo}

            self._iterationinfo.append(data)

            if(robO >= threshold): break

            q_ipo_new = self.recurrence(x,self._struct_q)
            cons = np.append(cons,{'type': 'ineq', 'fun':self.robustSample, 'args':(q_ipo_new,)})

        if (len(self._iterationinfo) == maxIterations and self._iterationinfo[maxIterations-1]['robOptInfo']["robustObj"]<threshold):
            import json
            j = json.dumps(self._iterationinfo,indent=4, sort_keys=True)
            raise Exception(j+"\nCould not find a robust objective")
        self._pcoeff = np.array(self._iterationinfo[len(self._iterationinfo)-1]["pcoeff"])
        self._qcoeff = np.array(self._iterationinfo[len(self._iterationinfo)-1]["qcoeff"])

    def multipleRestartForIterRobO(self, coeffs, maxRestarts=10, threshold=0.2, solver="L-BFGS-B"):
        minx, restartInfo = [], []
        totaltime, norestarts = 0, 0

        minrobO = np.inf
        for r in range(maxRestarts):
            x, robO, optstatus = self.restartRobO(coeffs,threshold,solver,r)
            totaltime += optstatus['time']

            if(minrobO > robO):
                minrobO = robO
                minx = x
            rinfo = {'robustArg':x.tolist(),'robustObj':robO, 'log':optstatus}
            restartInfo.append(rinfo)
            norestarts += 1
            if(robO < threshold):
                break
        restartInfo.append({'log':{'time':totaltime, 'noRestarts':norestarts}})
        return minx, minrobO, restartInfo

    def restartRobO(self, coeffs, threshold, solver, r):
        x0 = []
        if(r == 0):
            x0 = np.array([(self.box[i][0]+self.box[i][1])/2 for i in range(self.dim)], dtype=np.float64)
        else:
            x0 = np.zeros(self.dim, dtype=np.float64)
            for d in range(self.dim):
                x0[d] = np.random.rand()*(self.box[d][1]-self.box[d][0])+self.box[d][0]
        start = timer()
        ret = minimize(self.robustObj, x0, bounds=self.box, args = (coeffs,),method = solver, options={'maxiter': 1000,'ftol': 1e-4, 'disp': False})
        end = timer()
        optstatus = {'message':ret.get('message').decode(),'status':ret.get('status'),'noOfIterations':ret.get('nit'),'time':end-start}

        x = ret.get('x')
        robO = ret.get('fun')
        return x, robO, optstatus

    def robustSample(self, coeff, q_ipo):
        return np.sum([coeff[i]*q_ipo[i-self.M] for i in range(self.M,self.M+self.N)]) - 1

    def robustObj(self, x, coeff):
        q_ipo = self.recurrence(x, self._struct_q)
        return np.dot(coeff[self.M:], q_ipo)