import torch
import xitorch as xt
import xitorch.optimize
import xitorch.linalg
from ddft.qccalcs.base_qccalc import BaseQCCalc
from ddft.eks import BaseEKS, VKS, Hartree, xLDA
from ddft.utils.misc import set_default_option

__all__ = ["dft"]

class dft(BaseQCCalc):
    """
    Perform the restricted Kohn-Sham DFT.
    """
    def __init__(self, system, eks_model="lda", vext_fcn=None,
            # arguments for scf run
            dm0=None, eigen_options={}, fwd_options=None, bck_options=None):

        # extract properties of the system
        self.system = system
        self.numel = self.system.get_numel(split=False)
        self.norb = int(self.numel / 2.)
        self.grid = self.system._get_grid()
        self.hmodel = self.system._get_hamiltonian()
        self.dtype = self.system.dtype
        self.device = self.system.device

        # set the forward and backward options
        if fwd_options is None:
            fwd_options = {
                "method": "broyden1",
                "alpha": -0.5,
                "maxiter": 50,
            }
        if bck_options is None:
            bck_options = {}

        # setup the external potential
        self.vext = self.__get_vext(vext_fcn)

        # set up the vhks
        eks_model = self.__get_eks_model(eks_model)
        self.eks_model = eks_model + Hartree()
        self.vks_model = VKS(self.eks_model, self.grid)

        # set up the eigen module for the forward pass and scf module
        eigen_options = set_default_option(
            self.__get_default_eigen_options(),
            eigen_options)
        bck_options = set_default_option(
            self.__get_default_solve_options(),
            bck_options)
        self.eigen_options = eigen_options

        # set up the initial density before running scf module
        if dm0 is None:
            fock0 = self.__get_init_fock()
        else:
            fock0 = self.__dm_to_fock(dm0)
        nbatch, nbasis_tot, _ = fock0.shape
        y0 = fock0

        # run the self-consistent iterations
        y0 = y0.view(nbatch, -1)  # flatten the initial values
        yout = xitorch.optimize.equilibrium(
            fcn = self.__forward_pass2,
            y0 = y0,
            bck_options = {**bck_options, "verbose": True},
            verbose = True,
            **fwd_options) # (nbatch, nbasis_tot*nbasis_tot)
        yout = yout.view(nbatch, nbasis_tot, nbasis_tot) # (nbatch, nbasis_tot, nbasis_tot)

        self.scf_dm = self.__fock_to_dm(yout)
        self.scf_density = self.hmodel.dm2dens(self.scf_dm).density

        # postprocess properties
        self.scf_energy = None

    ######################### postprocess functions #########################
    def energy(self):
        if self.scf_energy is None:
            # calculate the total potential experienced by Kohn-Sham particles
            # from the last forward calculation
            density = self.scf_density
            eigvals, eigvecs, vks = self.__diagonalize(density)

            # calculates the Kohn-Sham energy
            eks_density = self.eks_model(density) # energy density (nbatch, nr)
            Eks = self.grid.integralbox(eks_density, dim=-1) # (nbatch,)

            # calculate the individual non-interacting particles energy
            sum_eigvals = 2 * eigvals.sum(dim=-1) # (nbatch,)
            vks_integral = self.grid.integralbox(vks*density, dim=-1)

            # compute the interacting particles energy
            Etot = sum_eigvals - vks_integral + Eks + self.system.get_nuclei_energy()
            self.scf_energy = Etot
        return self.scf_energy

    def density(self, gridpts=None):
        if gridpts is None:
            return self.scf_density

    def __forward_pass2(self, fock):
        # reshape the fock matrix to its proper size
        nbatch = fock.shape[0]
        fock = fock.view(nbatch, *self.hmodel.shape)

        new_dm = self.__fock_to_dm(fock)
        new_fock = self.__dm_to_fock(new_dm)

        return new_fock.view(nbatch, -1)

    def __fock_to_dm(self, fock):
        # diagonalize
        rparams = ()
        eigvals, eigvecs = xitorch.linalg.lsymeig(
            A = xt.LinearOperator.m(fock, is_hermitian=True),
            neig = self.norb,
            M = self.hmodel.get_overlap(*rparams),
            **self.eigen_options)

        # calculate the new density and the HKS potential
        dm = torch.einsum("...po,...qo->...pq", eigvecs, eigvecs)
        dm = self.__normalize_dm(dm)
        return dm

    def __dm_to_fock(self, dm):
        densinfo = self.hmodel.dm2dens(
            dm,
            calc_gradn = self.vks_model.need_gradn,
        )
        vks = self.vks_model(
            density = densinfo.density,
            gradn = densinfo.gradn,
        )
        vext_tot = self.vext + vks

        # get the new fock matrix
        hparams = (vext_tot,)
        fock = self.hmodel.get_hamiltonian(*hparams).fullmatrix()
        return fock

    def __normalize_dm(self, dm): # batchified
        # normalize the new density matrix
        # dm: (*BM, nbasis_tot, nbasis_tot)
        dens = self.hmodel.dm2dens(dm).density # (*BD, nr)
        dens_tot = self.grid.integralbox(dens, dim=-1, keepdim=True) # (*BD, 1)
        normfactor = self.numel / dens_tot # (*BD, 1)
        dm = dm * normfactor.unsqueeze(-1) # (*BMD, nbasis_tot, nbasis_tot)
        return dm # (*BMD, nbasis_tot, nbasis_tot)

    def __diagonalize(self, density):
        # calculate the total potential experienced by Kohn-Sham particles
        vks = self.vks_model(density) # (nbatch, nr)
        vext_tot = self.vext + vks

        # compute the eigenpairs
        # evals: (nbatch, norb), evecs: (nbatch, nr, norb)
        hparams = (vext_tot,)
        rparams = []
        eigvals, eigvecs = xitorch.linalg.lsymeig(
            A = self.hmodel.get_hamiltonian(*hparams),
            neig = self.norb,
            M = self.hmodel.get_overlap(*rparams),
            **self.eigen_options)
        # eigvals, eigvecs = self.eigen_model(hparams, rparams=rparams)

        return eigvals, eigvecs, vks

    ############# parameters setup functions #############
    def __get_eks_model(self, eks_model):
        if type(eks_model) == str:
            if eks_model == "lda":
                return xLDA()
            else:
                raise RuntimeError("Unknown eks model: %s" % eks_model)
        elif isinstance(eks_model, BaseEKS):
            return eks_model
        else:
            raise TypeError("eks_model must be a BaseEKS or a string")

    def __get_init_fock(self):
        vext_tot = self.vext
        hparams = (vext_tot,)
        fock0 = self.hmodel.get_hamiltonian(*hparams).fullmatrix()

        # do one forward pass
        fock0shape = fock0.shape
        nbatch = fock0shape[0]
        fock0 = self.__forward_pass2(fock0.view(nbatch, -1)).view(fock0shape)
        return fock0

    def __get_vext(self, vext_fcn):
        if vext_fcn is not None:
            vext = vext_fcn(self.grid.rgrid).unsqueeze(0) # (nbatch=1, nr)
        else:
            vext = torch.zeros_like(self.grid.rgrid[:,0]).unsqueeze(0) # (nbatch, nr)
        return vext

    def __get_default_eigen_options(self):
        nsize = self.hmodel.shape[-1]*self.hmodel.shape[-2]
        defopt = {}
        if nsize < 100:
            defopt["method"] = "exacteig"
        return defopt

    def __get_default_solve_options(self):
        nsize = self.hmodel.shape[-1]*self.hmodel.shape[-2]
        defopt = {}
        if nsize < 20:
            defopt["method"] = "exactsolve"
        else:
            defopt["method"] = None  # let xitorch decides
        return defopt

    ############################# editable module #############################
    def getparamnames(self, methodname, prefix=""):
        if methodname == "__forward_pass2":
            return self.getparamnames("__fock_to_dm", prefix=prefix) + \
                   self.getparamnames("__dm_to_fock", prefix=prefix)
        elif methodname == "__fock_to_dm":
            return self.getparamnames("__normalize_dm", prefix=prefix) + \
                   self.hmodel.getparamnames("get_overlap", prefix=prefix+"hmodel.")
        elif methodname == "__dm_to_fock":
            return [prefix + "vext"] + \
                   self.hmodel.getparamnames("dm2dens", prefix=prefix+"hmodel.") + \
                   self.vks_model.getparamnames("__call__", prefix=prefix+"vks_model.") + \
                   self.hmodel.getparamnames("get_hamiltonian", prefix=prefix+"hmodel.")
        elif methodname == "__diagonalize":
            return [prefix+"vext"] + \
                   self.hmodel.getparamnames("get_hamiltonian", prefix=prefix+"hmodel.") + \
                   self.hmodel.getparamnames("get_overlap", prefix=prefix+"hmodel.") + \
                   self.vks_model.getparamnames("__call__", prefix=prefix+"vks_model.")
        elif methodname == "__normalize_dm":
            return [prefix+"numel"] + \
                   self.grid.getparamnames("integralbox", prefix=prefix+"grid.")
        else:
            raise KeyError("getparamnames has no %s method" % methodname)