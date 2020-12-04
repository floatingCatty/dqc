import torch
from dataclasses import dataclass
from typing import Optional, Union, List

__all__ = ["CGTOBasis", "AtomCGTOBasis", "ValGrad"]

@dataclass
class CGTOBasis:
    angmom: int
    alphas: torch.Tensor  # (nbasis,)
    coeffs: torch.Tensor  # (nbasis,)

@dataclass
class AtomCGTOBasis:
    atomz: int
    bases: List[CGTOBasis]
    pos: torch.Tensor  # (ndim,)

@dataclass
class ValGrad:
    value: torch.Tensor  # torch.Tensor of the value in the grid
    grad: Optional[torch.Tensor] = None  # torch.Tensor representing (gradx, grady, gradz) with shape
    # ``(..., 3)``
    lapl: Optional[torch.Tensor] = None  # torch.Tensor of the laplace of the value

def _add_densinfo(a: ValGrad, b: ValGrad) -> ValGrad:
    return ValGrad(
        value=a.value + b.value,
        grad=a.grad + b.grad if a.grad is not None else None,
        lapl=a.lapl + b.lapl if a.lapl is not None else None,
    )

def _mul_densinfo(a: ValGrad, f: Union[float, int]) -> ValGrad:
    return ValGrad(
        value=a.value * f,
        grad=a.grad * f if a.grad is not None else None,
        lapl=a.lapl * f if a.lapl is not None else None,
    )

ValGrad.__add__ = _add_densinfo  # type: ignore
ValGrad.__mul__ = _mul_densinfo  # type: ignore
