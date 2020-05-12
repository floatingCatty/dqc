from abc import abstractmethod
import torch
import numpy as np
from numpy.polynomial.legendre import leggauss
from ddft.grids.base_grid import BaseGrid, BaseTransformed1DGrid
from ddft.utils.legendre import legint, legvander, legder, deriv_legval
from ddft.utils.interp import CubicSpline
from ddft.grids.radialtransform import ShiftExp

class LegendreRadialTransform(BaseTransformed1DGrid):
    def __init__(self, nx, transformobj, dtype=torch.float, device=torch.device('cpu')):
        # cache variables
        self._spline_mat_inv_ = None

        xleggauss, wleggauss = leggauss(nx)
        self.xleggauss = torch.tensor(xleggauss, dtype=dtype, device=device)
        self.wleggauss = torch.tensor(wleggauss, dtype=dtype, device=device)
        self._boxshape = (nx,)
        self.interpolator = CubicSpline(self.xleggauss)

        self.transformobj = transformobj
        self.rs = self.transformobj.transform(self.xleggauss)
        self._rgrid = self.rs.unsqueeze(-1) # (nx, 1)

        # integration elements
        self._scaling = self.transformobj.get_scaling(self.rs) # dr/dg
        self._dr = self._scaling * self.wleggauss
        self._dvolume = (4*np.pi*self.rs*self.rs) * self._dr

        # legendre basis (from tinydft/tinygrid.py)
        self.basis = legvander(self.xleggauss, nx-1, orderfirst=True) # (nr, nr)
        self.inv_basis = self.basis.inverse()

        # # construct the differentiation matrix
        # dlegval = deriv_legval(self.xleggauss, nx)
        # eye = torch.eye(nx, dtype=dtype, device=device)
        # dxleg = self.xleggauss - self.xleggauss.unsqueeze(-1) + eye
        # dmat = dlegval / (dlegval.unsqueeze(-1) * dxleg) # (nr, nr)
        # dmat_diag = self.xleggauss / (1. - self.xleggauss) / (1 + self.xleggauss) # (nr,)
        # self.diff_matrix = dmat * (1.-eye) + torch.diag_embed(dmat_diag)

    def get_dvolume(self):
        return self._dvolume

    def solve_poisson(self, f):
        # f: (nbatch, nr)
        # the expression below is used to satisfy the following conditions:
        # * symmetric operator (by doing the integral 1/|r-r1|)
        # * 0 at r=\infinity, but not 0 at the bound (again, by doing the integral 1/|r-r1|)
        # to satisfy all the above, we choose to do the integral of
        #     Vlm(r) = integral_rmin^rmax (rless^l) / (rgreat^(l+1)) flm(r1) r1^2 dr1
        # where rless = min(r,r1) and rgreat = max(r,r1)

        # calculate the matrix rless / rgreat
        rless = torch.min(self.rs.unsqueeze(-1), self.rs) # (nr, nr)
        rgreat = torch.max(self.rs.unsqueeze(-1), self.rs)
        rratio = 1. / rgreat

        # the integralbox for radial grid is integral[4*pi*r^2 f(r) dr] while here
        # we only need to do integral[f(r) dr]. That's why it is divided by (4*np.pi)
        # and it is not multiplied with (self.radrgrid**2) in the lines below
        intgn = (f).unsqueeze(-2) * rratio # (nbatch, nr, nr)
        vrad_lm = self.integralbox(intgn / (4*np.pi), dim=-1)

        return -vrad_lm

    @property
    def rgrid(self):
        return self._rgrid

    @property
    def boxshape(self):
        return self._boxshape

    @property
    def rgrid(self):
        return self._rgrid

    def interpolate(self, f, rq, extrap=None):
        # f: (nbatch, nr)
        # rq: (nrq, ndim)
        # return: (nbatch, nrq)
        nbatch, nr = f.shape
        nrq = rq.shape[0]

        rmax = self.rgrid.max()
        idxinterp = rq[:,0] <= rmax
        idxextrap = rq[:,0] > rmax
        allinterp = torch.all(idxinterp)
        if allinterp:
            rqinterp = rq[:,0]
        else:
            rqinterp = rq[idxinterp,0]

        # doing the interpolation
        # cubic interpolation is slower, but more robust on backward gradient
        xq = self.transformobj.invtransform(rqinterp) # (nrq,)
        frqinterp = self.interpolator.interp(f, xq)
        # coeff = torch.matmul(f, self.inv_basis) # (nbatch, nr)
        # basis = legvander(xq, nr-1, orderfirst=True)
        # frqinterp = torch.matmul(coeff, basis)

        if allinterp:
            return frqinterp

        # extrapolate
        if extrap is not None:
            frqextrap = extrap(rq[idxextrap,:])

        # combine the interpolation and extrapolation
        frq = torch.zeros((nbatch, nrq), dtype=rq.dtype, device=rq.device)
        frq[:,idxinterp] = frqinterp
        if extrap is not None:
            frq[:,idxextrap] = frqextrap

        return frq

    def grad(self, p, dim=-1, idim=0):
        if dim != -1:
            p = p.transpose(dim, -1) # (..., nr)

        # get the derivative w.r.t. the legendre basis
        coeff = torch.matmul(p, self.inv_basis) # (..., nr)
        dcoeff = legder(coeff) # (..., nr)
        dpdq = torch.matmul(dcoeff, self.basis) # (..., nr)
        # # multiply with the differentiation matrix to get dp/dq
        # dpdq = torch.matmul(p, self.diff_matrix)

        # get the derivative w.r.t. r
        dpdr = dpdq / self.transformobj.get_scaling(self.rgrid[:,0])
        if dim != -1:
            dpdr = dpdr.transpose(dim, -1)
        return dpdr

    def laplace(self, p, dim=-1):
        if dim != -1:
            p = p.transpose(dim, -1) # p: (..., nr)

        pder1 = self.grad(p)
        pder2 = self.grad(pder1)
        res = pder2 + 2 * pder1 / (self.rs + 1e-15)

        if dim != -1:
            res = res.transpose(dim, -1)
        return res

    def getparams(self, methodname):
        if methodname == "interpolate":
            return self.transformobj.getparams("invtransform") + \
                   self.interpolator.getparams("interp")
        elif methodname == "solve_poisson":
            return [self.rs, self._dvolume]
        elif methodname == "get_dvolume":
            return [self._dvolume]
        else:
            raise RuntimeError("The method %s has not been specified for getparams" % methodname)

    def setparams(self, methodname, *params):
        if methodname == "interpolate":
            idx = 0
            idx += self.transformobj.setparams("invtransform", *params[idx:])
            idx += self.interpolator.setparams("interp", *params[idx:])
            return idx
        elif methodname == "solve_poisson":
            self.rs, self._dvolume = params[:2]
            return 2
        elif methodname == "get_dvolume":
            self._dvolume, = params[:1]
            return 1
        else:
            raise RuntimeError("The method %s has not been specified for setparams" % methodname)

class LegendreRadialShiftExp(LegendreRadialTransform):
    def __init__(self, rmin, rmax, nr, dtype=torch.float, device=torch.device('cpu')):
        # setup the parameters needed for the transformation
        transformobj = ShiftExp(rmin, rmax, nr, dtype=dtype, device=device)
        super(LegendreRadialShiftExp, self).__init__(nr, transformobj, dtype=dtype, device=device)

if __name__ == "__main__":
    import lintorch as lt
    grid = LegendreRadialShiftExp(1e-4, 1e2, 100, dtype=torch.float64)
    rgrid = grid.rgrid.clone().detach()
    f = torch.exp(-rgrid[:,0].unsqueeze(0)**2*0.5)

    lt.list_operating_params(grid.solve_poisson, f)
    lt.list_operating_params(grid.interpolate, f, rgrid)
    lt.list_operating_params(grid.get_dvolume)
