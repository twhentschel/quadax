"""Romberg integration aka adaptive trapezoid with Richardson extrapolation."""

from functools import partial

import jax
import jax.numpy as jnp

from .utils import QuadratureInfo, map_interval, wrap_func


def tanhsinh_transform(fun):
    """Transform a function by mapping with tanh-sinh."""
    xk = lambda t: jnp.tanh(jnp.pi / 2 * jnp.sinh(t))
    wk = lambda t: jnp.pi / 2 * jnp.cosh(t) / jnp.cosh(jnp.pi / 2 * jnp.sinh(t)) ** 2
    func = lambda t, *args: fun(xk(t), *args) * wk(t)
    return jax.jit(func)


def get_tmax(xmax):
    """Inverse of tanh-sinh transform."""
    tanhinv = jax.jit(lambda x: 1 / 2 * jnp.log((1 + x) / (1 - x)))
    sinhinv = jax.jit(lambda x: jnp.log(x + jnp.sqrt(x**2 + 1)))
    return sinhinv(2 / jnp.pi * tanhinv(xmax))


@partial(jax.custom_jvp, nondiff_argnums=(0,))
def romberg(
    fun,
    a,
    b,
    args=(),
    full_output=False,
    epsabs=1.4e-8,
    epsrel=1.4e-8,
    divmax=20,
    extrap=True,
):
    """Romberg integration of a callable function or method.

    Returns the integral of `fun` (a function of one variable)
    over the interval (`a`, `b`).

    Good for non-smooth or piecewise smooth integrands.

    Not recommended for infinite intervals, or functions with singularities.

    Algorithm is copied from SciPy, but in practice tends to underestimate the error
    for even mildly bad integrands, sometimes by several orders of magnitude.

    Parameters
    ----------
    fun : callable
        Function to integrate, should have a signature of the form
        ``fun(x, *args)`` -> float. Should be JAX transformable.
    a, b : float
        Lower and upper limits of integration. Use np.inf to denote infinite intervals.
    args : tuple
        additional arguments passed to fun
    full_output : bool, optional
        If True, return the full state of the integrator. See below for more
        information.
    epsabs, epsrel : float
        Absolute and relative tolerances. If I1 and I2 are two
        successive approximations to the integral, algorithm terminates
        when abs(I1-I2) < max(epsabs, epsrel*|I2|)
    divmax : int, optional
        Maximum order of extrapolation. Default is 10.
        Total number of function evaluations will be at
        most 2**divmax + 1
    extrap : bool, optional
        Whether to perform Richardson extrapolation.

    Returns
    -------
    y  : float
        Approximation to the integral
    info : QuadratureInfo
        Named tuple with the following fields:

        * err : (float) Estimate of the error in the approximation.
        * neval : (int) Total number of function evaluations.
        * status : (int) Flag indicating reason for termination. status of 0 means
          normal termination, any other value indicates a possible error. A human
          readable message can be obtained by ``print(quadax.STATUS[status])``
        * info : (dict or None) Other information returned by the algorithm.
          Only present if ``full_output`` is True. Contains the following:

          * table : (ndarray, size(dixmax+1, divmax+1)) Estimate of the integral
            from each level of discretization and each step of extrapolation.

    """
    # map a, b -> [-1, 1]
    fun = map_interval(fun, a, b)
    vfunc = wrap_func(fun, args)
    a, b = -1, 1

    result = jnp.zeros((divmax + 1, divmax + 1))
    result = result.at[0, 0].set(vfunc(a) + vfunc(b))
    neval = 2
    err = jnp.inf
    state = (result, 1, neval, err)

    def ncond(state):
        result, n, neval, err = state
        return (n < divmax + 1) & (err > jnp.maximum(epsabs, epsrel * result[n, n]))

    def nloop(state):
        result, n, neval, err = state
        h = (b - a) / 2**n
        s = 0.0

        def sloop(i, s):
            s += vfunc(a + h * (2 * i - 1))
            return s

        result = result.at[n, 0].set(
            0.5 * result[n - 1, 0]
            + h * jax.lax.fori_loop(1, (2**n) // 2 + 1, sloop, s)
        )
        neval += (2**n) // 2

        def mloop(m, result):
            temp = 1 / (4.0**m - 1.0) * (result[n, m - 1] - result[n - 1, m - 1])
            result = result.at[n, m].set(result[n, m - 1] + temp)
            return result

        if extrap:
            result = jax.lax.fori_loop(1, n + 1, mloop, result)
            err = abs(result[n, n] - result[n, n - 1])
        else:
            err = abs(result[n, 0] - result[n - 1, 0])
        return result, n + 1, neval, err

    result, n, neval, err = jax.lax.while_loop(ncond, nloop, state)

    y = result[n - 1, n - 1] if extrap else result[n - 1, 0]
    status = 2 * (err > jnp.maximum(epsabs, epsrel * y))
    info = result if full_output else None
    out = QuadratureInfo(err, neval, status, info)
    return y, out


@romberg.defjvp
def _romberg_jvp(fun, primals, tangents):
    a, b, args = primals[:3]
    adot, bdot, argsdot = tangents[:3]
    f1, info1 = romberg(fun, *primals)

    def df(x, *args):
        return jax.jvp(fun, (x, *args), (jnp.zeros_like(x), *argsdot))[1]

    f2, info2 = romberg(df, *primals)
    return (f1, info1), (fun(b, *args) * bdot - fun(a, *args) * adot + f2, info2)


@partial(jax.custom_jvp, nondiff_argnums=(0,))
def rombergts(
    fun, a, b, args=(), full_output=False, epsabs=1.4e-8, epsrel=1.4e-8, divmax=20
):
    """Romberg integration with tanh-sinh (aka double exponential) transformation.

    Returns the integral of `fun` (a function of one variable)
    over the interval (`a`, `b`).

    Performs well for functions with singularities at the endpoints or integration
    over infinite intervals. May be slightly less efficient than ``quadgk`` or
    ``quadcc`` for smooth integrands.

    Parameters
    ----------
    fun : callable
        Function to integrate, should have a signature of the form
        ``fun(x, *args)`` -> float. Should be JAX transformable.
    a, b : float
        Lower and upper limits of integration. Use np.inf to denote infinite intervals.
    args : tuple
        additional arguments passed to fun
    full_output : bool, optional
        If True, return the full state of the integrator. See below for more
        information.
    epsabs, epsrel : float
        Absolute and relative tolerances. If I1 and I2 are two
        successive approximations to the integral, algorithm terminates
        when abs(I1-I2) < max(epsabs, epsrel*|I2|)
    divmax : int, optional
        Maximum order of extrapolation. Default is 10.
        Total number of function evaluations will be at
        most 2**divmax + 1

    Returns
    -------
    y  : float
        Approximation to the integral
    info : QuadratureInfo
        Named tuple with the following fields:

        * err : (float) Estimate of the error in the approximation.
        * neval : (int) Total number of function evaluations.
        * status : (int) Flag indicating reason for termination. status of 0 means
          normal termination, any other value indicates a possible error. A human
          readable message can be obtained by ``print(quadax.STATUS[status])``
        * info : (dict or None) Other information returned by the algorithm.
          Only present if ``full_output`` is True. Contains the following:

          * table : (ndarray, size(dixmax+1, divmax+1)) Estimate of the integral
            from each level of discretization and each step of extrapolation.

    """
    # map a, b -> [-1, 1]
    fun = map_interval(fun, a, b)
    # map [-1, 1] to [-inf, inf], but with mass concentrated near 0
    fun = tanhsinh_transform(fun)
    func = wrap_func(fun, args)
    # we generally only need to integrate ~[-3, 3] or ~[-4, 4]
    # we don't want to include the endpoint that maps to x==1 to avoid
    # possible singularities, so we find the largest t s.t. x(t) < 1
    # and use that as our interval
    # inverse of tanh-sinh transformation for x = 1-eps
    tmax = get_tmax(jnp.array(1.0) - 10 * jnp.finfo(jnp.array(1.0)).eps)
    a, b = -tmax, tmax
    return romberg(func, a, b, (), full_output, epsabs, epsrel, divmax, extrap=False)


@rombergts.defjvp
def _rombergts_jvp(fun, primals, tangents):
    a, b, args = primals[:3]
    adot, bdot, argsdot = tangents[:3]
    f1, info1 = rombergts(fun, *primals)

    def df(x, *args):
        return jax.jvp(fun, (x, *args), (jnp.zeros_like(x), *argsdot))[1]

    f2, info2 = rombergts(df, *primals)
    return (f1, info1), (fun(b, *args) * bdot - fun(a, *args) * adot + f2, info2)
