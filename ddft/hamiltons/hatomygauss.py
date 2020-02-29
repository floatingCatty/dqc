from abc import abstractmethod, abstractproperty
from functools import reduce
import torch
import numpy as np
import lintorch as lt

from ddft.hamiltons.base_hamilton import BaseHamilton
from ddft.utils.spharmonics import spharmonics

class HamiltonAtomYGauss(BaseHamilton):
    """
    HamiltonAtomYGauss represents the system of one atom with all-electrons
    potential at the centre of coordinate. The chosen basis is
    well-tempered Gaussian radial basis set with spherical harmonics.

    Arguments
    ---------
    * grid: BaseGrid
        The integration grid.
    * gwidths: torch.tensor (ng,)
        The tensor of Gaussian-widths of the basis.
    * maxangmom: int
        The maximum angular momentum of the Hamiltonian

    Forward arguments
    -----------------
    * wf: torch.tensor (nbatch, ns, ncols)
        The basis coefficients of the radial wavefunction at the given
        angular momentum.
    * vext: torch.tensor (nbatch, nr)
        External potential other than the potential from the central atom.
    * atomz: torch.tensor (nbatch,)
        The atomic number of the central atom.

    Overlap arguments
    -----------------
    * wf: torch.tensor (nbatch, ns, ncols)
        The basis coefficients of the radial wavefunction at the given
        angular momentum.

    Note
    ----
    * To get the best accuracy, the gaussian width range should be well inside
        the radial grid range.
    """

    def __init__(self, grid, gwidths,
                       maxangmom=5):
        ng = gwidths.shape[0]
        nsh = (maxangmom+1)**2
        ns = int(ng*nsh)
        self.ng = ng
        self.nsh = nsh
        self._grid = grid
        dtype = gwidths.dtype
        device = gwidths.device
        super(HamiltonAtomYGauss, self).__init__(
            shape = (ns, ns),
            is_symmetric = True,
            is_real = True,
            dtype = dtype,
            device = device)

        # well-tempered gaussian factor from tinydft
        self.gwidths = gwidths # torch.nn.Parameter(gwidths) # (ng)
        rgrid = grid.rgrid # (nr,ndim)
        self.rs = grid.radial_grid.rgrid[:,0] # (nrad)
        nrad = self.rs.shape[0]
        self.maxangmom = maxangmom

        # get the radial basis in rgrid
        # (ng, nrad)
        gw1 = self.gwidths.unsqueeze(-1) # (ng, 1)
        unnorm_radbasis = torch.exp(-self.rs*self.rs / (2*gw1*gw1)) * self.rs # (nrad,)
        radnorm = np.sqrt(2./3) / gw1**2.5 / np.pi**.75 # (ng, 1)
        self.radbasis = radnorm * unnorm_radbasis # (ng, nrad)

        # get the angular basis in rgrid (nsh, nphitheta)
        rgrid1 = rgrid.view(nrad,-1,rgrid.shape[-1]) # (nrad, nphitheta, ndim)
        phi = rgrid1[0,:,1] # (nphitheta,)
        theta = rgrid1[0,:,2] # (nphitheta,)
        costheta = torch.cos(theta) # (nphitheta,)
        self.angbasis = spharmonics(costheta, phi, maxangmom)

        self.basis = self.radbasis.unsqueeze(1).unsqueeze(-1) * self.angbasis.unsqueeze(1) # (ng, nsh, nrad, nphitheta)
        self.basis = self.basis.view(self.ng*self.nsh, -1) # (ns, nr)
        self.basis_dvolume = self.basis * self.grid.get_dvolume() # (ns, nr)
        # print(self.grid.integralbox(self.basis*self.basis)-1)
        # raise RuntimeError

        # construct the matrices provided ng is small enough
        gwprod = gw1 * self.gwidths
        # gwprod32 = gwprod**1.5
        gwprod12 = gwprod**0.5
        gwprod52 = gwprod**2.5
        gw2sum = gw1*gw1 + self.gwidths*self.gwidths
        gwnet2 = gwprod*gwprod / gw2sum
        gwnet = torch.sqrt(gwnet2)
        gwpoly = 2*gw1**4 - 11*gw1*gw1*self.gwidths*self.gwidths + 2*self.gwidths**4

        # (ng,ng)
        olp = 4 * np.sqrt(2) * gwnet**5 / gwprod52
        coul = -16./(3*np.sqrt(np.pi)) * gwnet**4 / gwprod52
        kin_ang = 2 * np.sqrt(2) / 3 * gwnet**3 / gwprod52
        kin_rad = -2 * np.sqrt(2) / 3 * gwnet**3 / gw2sum**2 / gwprod52 * gwpoly

        # create the batch dimension to the matrix to enable batched matmul
        # shape: (1, ng, ng)
        self.kin_rad = kin_rad.unsqueeze(0)
        self.kin_ang = kin_ang.unsqueeze(0)
        self.olp = olp.unsqueeze(0)
        self.coul = coul.unsqueeze(0)

        # create the angular momentum factor
        lhat = []
        eps = 1e-9
        eps2 = 1e-8
        for angmom in range(maxangmom+1):
            for j in range(-angmom, angmom+1):
                # small noise epsilon to avoid degeneracy and improve the
                # numerical stability in avoiding complex eigenvalues
                noise = j * eps + angmom * eps2
                lhat.append(angmom*(angmom+1)+noise)
            # lhat = lhat + [angmom*(angmom+1)]*(2*angmom+1)
        self.lhat = torch.tensor(lhat, dtype=dtype, device=device) # (nsh,)

    ############################# basis part #############################
    def forward(self, wf, vext, atomz):
        # wf: (nbatch, ns, ncols)
        # vext: (nbatch, nr)
        # atomz: (nbatch,)

        nbatch, ns, ncols = wf.shape

        # get the part that does not depend on angle (kin_rad and coulomb)
        wf = wf.contiguous()
        wf1 = wf.view(nbatch, self.ng, -1) # (nbatch, ng, nsh*ncols)
        kin_rad_coul_mat = self.kin_rad + self.coul * atomz.unsqueeze(-1).unsqueeze(-2)
        kin_rad_coul = torch.matmul(kin_rad_coul_mat, wf1)

        # get the angular momentum part
        kin_ang1 = torch.matmul(self.kin_ang, wf1) # (nbatch, ng, nsh*ncols)
        kin_ang2 = kin_ang1.view(nbatch*self.ng, self.nsh, ncols) * self.lhat.unsqueeze(-1) # (nbatch*ng, nsh, ncols)
        kin_ang = kin_ang2.view(nbatch, self.ng, self.nsh*ncols)

        # vext part
        # self.basis: (ns, nr)
        # extpot: (nbatch, ns, ns)
        extpot = torch.matmul(vext.unsqueeze(1) * self.basis_dvolume, self.basis.transpose(-2,-1))
        # extpot = self.grid.mmintegralbox(vext.unsqueeze(1) * self.basis, self.basis.transpose(-2,-1))
        extpot = torch.bmm(extpot, wf) # (nbatch, ns, ncols)

        hwf = kin_rad_coul + kin_ang # (nbatch, ng, nsh*ncols)
        hwf = hwf.view(nbatch, -1, ncols) + extpot
        return hwf

    def precond(self, y, vext, atomz, biases=None, M=None, mparams=None):
        return y # ???

    def _overlap(self, wf):
        nbatch, ns, ncols = wf.shape
        wf = wf.view(nbatch, self.ng, -1) # (nbatch, ng, nsh*ncols)
        res = torch.matmul(self.olp, wf)  # (nbatch, ng, nsh*ncols)
        return res.view(nbatch, -1, ncols)

    def torgrid(self, wfs, dim=-2):
        # wfs: (..., ns, ...)
        wfs = wfs.transpose(dim, -1) # (..., ns)
        wfr = torch.matmul(wfs, self.basis) # (..., nr)
        return wfr.transpose(dim, -1)

    ############################# grid part #############################
    @property
    def grid(self):
        return self._grid

if __name__ == "__main__":
    from ddft.grids.radialshiftexp import LegendreRadialShiftExp
    from ddft.grids.sphangulargrid import Lebedev

    dtype = torch.float64
    gwidths = torch.logspace(np.log10(1e-5), np.log10(1e2), 60).to(dtype)
    radgrid = LegendreRadialShiftExp(1e-6, 1e4, 200, dtype=dtype)
    grid = Lebedev(radgrid, prec=13, basis_maxangmom=4, dtype=dtype)
    nr = grid.rgrid.shape[0]
    h = HamiltonAtomYGauss(grid, gwidths, maxangmom=3).to(dtype)

    vext = torch.zeros(1, nr).to(dtype)
    atomz = torch.tensor([1.0]).to(dtype)
    H = h.fullmatrix(vext, atomz)
    olp = h.overlap.fullmatrix()
    print(torch.symeig(olp)[0])
    evals, evecs = torch.eig(torch.solve(H[0], olp[0])[0])
    evals = torch.sort(evals.view(-1))[0]
    print(evals[:20])
