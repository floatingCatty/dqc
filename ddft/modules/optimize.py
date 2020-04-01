import torch
import lintorch as lt
from ddft.utils.misc import set_default_option
from ddft.utils.fd import finite_differences

class OptimizationModule(torch.nn.Module):
    """
    OptimizationModule finds the optimum solution from a given model.

        z* = min_x model(x, y)
        x* = argmin_x model(x, y)

    The model should take the optimized parameters, x, and the external
    parameters, y.
    The variables `x` and `y` must be a list of tensor.
    When the model is wrapped with an OptimizationModule, the new module
    takes `y` as the input and produce `z*` and `x*` as the outputs.

    This module cannot propagate the gradient from `x*`.
    """
    def __init__(self, model, minimize=True, forward_options={}, backward_options={}):
        super(OptimizationModule, self).__init__()
        self.model = model
        self.fwd_options = forward_options
        self.bck_options = backward_options
        self.multiplier = 1.0 if minimize else -1.0

    def forward(self, x0, yparams):
        results = _ForwardOpt.apply(self.model, self.multiplier, x0, self.fwd_options, yparams)
        zopt = results[0]
        xopt = results[1:]
        if self.training:
            for p in yparams: p.requires_grad_()
            # zopt2 = self.model(xopt, yparams)
            # res = _BackwardOpt.apply(zopt2, self.bck_options, xopt)
            # zopt = res[0]
            # xopt = res[1:]
            zopt = self.model(xopt, yparams)
        return zopt, xopt

class _ForwardOpt(torch.autograd.Function):
    @staticmethod
    def forward(ctx, model, mult, x0, fwd_options, yparams):
        # set default options
        config = set_default_option({
            "max_niter": 100,
            "min_eps": 1e-6,
            "verbose": False,
            "lr": 1e-2,
            "method": "lbfgs",
        }, fwd_options)

        verbose = config["verbose"]

        # get the algorithm class
        method = config["method"].lower()
        if method == "lbfgs":
            opt_cls = torch.optim.LBFGS
            opt_kwds = ["lr"]
        elif method == "sgd":
            opt_cls = torch.optim.SGD
            opt_kwds = ["lr", "momentum", "dampening", "weight_decay", "nesterov"]

        opt_kwargs = {x:config[x] for x in opt_kwds if x in config}

        with torch.enable_grad():
            x = [p.detach().clone().requires_grad_() for p in x0]
            y = [p.detach().clone() for p in yparams]

            opt = opt_cls(x, **opt_kwargs)
            for i in range(config["max_niter"]):
                def closure():
                    opt.zero_grad()
                    z = model(x, y) * mult
                    z.backward()
                    if verbose and i%10 == 0:
                        print("Iter %3d: %.3e" % (i, z))
                    return z

                opt.step(closure)

        # reset all the gradients
        opt.zero_grad()
        for p in model.parameters():
            p.grad.zero_()

        xopt = x
        zopt = model(xopt, y)
        res = (zopt, *xopt)
        return res

    @staticmethod
    def backward(ctx, grad_zopt, *grad_xopt):
        return (None, None, None, None, None)

class _BackwardOpt(torch.autograd.Function):
    @staticmethod
    def forward(ctx, fmodel, bck_options, xopt):
        ctx.fmodel = fmodel
        ctx.bck_options = bck_options
        ctx.xopt = xopt
        res = (fmodel, *xopt)
        return res

    @staticmethod
    def backward(ctx, grad_zopt, *grad_xopt):
        grad_fmodel = grad_zopt
        allzeros = True
        for gx in grad_xopt:
            if not torch.allclose(gx, gx*0):
                allzeros = False
                break
        if allzeros:
            return (grad_fmodel, None, None)
        else:
            raise RuntimeError("Unimplemented gradient contribution from the argmin")

if __name__ == "__main__":
    class DummyModule(torch.nn.Module):
        def __init__(self, A):
            super(DummyModule, self).__init__()
            self.A = torch.nn.Parameter(A)

        def forward(self, x, y):
            # x: list of (nbatch, nr)
            # y: list of (nbatch, nr)
            y = y[0]
            x = x[0]
            nbatch = y.shape[0]
            tanh = torch.nn.Tanh()
            A = self.A.unsqueeze(0).expand(nbatch, -1, -1)
            Ax = torch.bmm(A, x.unsqueeze(-1)).squeeze(-1)
            Axy = Ax + y
            xnew = Axy**2 + 1.
            # xnew = tanh(0.1 * Axy)
            loss = ((x - xnew)**2).sum()
            return loss

    dtype = torch.float64
    fwd_options = {
        "max_niter": 60,
        "lr": 1e-2,
        "verbose": True,
        "method": "lbfgs",
    }
    nr = 3
    nbatch = 1
    torch.manual_seed(124)
    A  = torch.randn((nr, nr)).to(dtype)
    print(A)
    y  = torch.rand((nbatch, nr)).to(dtype).requires_grad_()
    x0 = torch.rand((nbatch, nr)).to(dtype).requires_grad_()

    model = DummyModule(A)
    optmodel = OptimizationModule(model, forward_options=fwd_options)
    minloss, yopt = optmodel((x0,), (y,))

    print("Forward results:")
    print(yopt)
    print("    should be close to 0:")
    print(minloss)

    def getloss(A, y, x0, return_model=False):
        model = DummyModule(A)
        optmodel = OptimizationModule(model, forward_options=fwd_options)
        minloss, yopt = optmodel((x0,), (y,))
        loss = minloss
        if not return_model:
            return loss
        else:
            return loss, model

    # gradient with backprop
    loss, model = getloss(A, y, x0, return_model=True)
    loss.backward()
    A_grad = list(model.parameters())[0].grad.data
    y_grad = y.grad.data

    # gradient with finite_differences
    with torch.no_grad():
        A_fd = finite_differences(getloss, (A, y, x0), 0, eps=1e-4)
        y_fd = finite_differences(getloss, (A, y, x0), 1, eps=1e-4)

    print("Gradient of A:")
    print(A_grad)
    print(A_fd)
    print("    should be close to 1:")
    print(A_grad / A_fd)

    print("Gradient of x:")
    print(y_grad)
    print(y_fd)
    print("    should be close to 1:")
    print(y_grad / y_fd)