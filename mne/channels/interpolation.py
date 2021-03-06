# Authors: Denis Engemann <denis.engemann@gmail.com>
#
# License: BSD (3-clause)

import numpy as np
from numpy.polynomial.legendre import legval
from scipy import linalg

from ..utils import logger
from ..io.pick import pick_types, pick_channels
from ..surface import _normalize_vectors
from ..bem import _fit_sphere
from ..forward import _map_meg_channels


def _calc_g(cosang, stiffness=4, num_lterms=50):
    """Calculate spherical spline g function between points on a sphere.

    Parameters
    ----------
    cosang : array-like of float, shape(n_channels, n_channels)
        cosine of angles between pairs of points on a spherical surface. This
        is equivalent to the dot product of unit vectors.
    stiffness : float
        stiffness of the spline.
    num_lterms : int
        number of Legendre terms to evaluate.

    Returns
    -------
    G : np.ndrarray of float, shape(n_channels, n_channels)
        The G matrix.
    """
    factors = [(2 * n + 1) / (n ** stiffness * (n + 1) ** stiffness *
                              4 * np.pi) for n in range(1, num_lterms + 1)]
    return legval(cosang, [0] + factors)


def _calc_h(cosang, stiffness=4, num_lterms=50):
    """Calculate spherical spline h function between points on a sphere.

    Parameters
    ----------
    cosang : array-like of float, shape(n_channels, n_channels)
        cosine of angles between pairs of points on a spherical surface. This
        is equivalent to the dot product of unit vectors.
    stiffness : float
        stiffness of the spline. Also referred to as `m`.
    num_lterms : int
        number of Legendre terms to evaluate.
    H : np.ndrarray of float, shape(n_channels, n_channels)
        The H matrix.
    """
    factors = [(2 * n + 1) /
               (n ** (stiffness - 1) * (n + 1) ** (stiffness - 1) * 4 * np.pi)
               for n in range(1, num_lterms + 1)]
    return legval(cosang, [0] + factors)


def _make_interpolation_matrix(pos_from, pos_to, alpha=1e-5):
    """Compute interpolation matrix based on spherical splines

    Implementation based on [1]

    Parameters
    ----------
    pos_from : np.ndarray of float, shape(n_good_sensors, 3)
        The positions to interpoloate from.
    pos_to : np.ndarray of float, shape(n_bad_sensors, 3)
        The positions to interpoloate.
    alpha : float
        Regularization parameter. Defaults to 1e-5.

    Returns
    -------
    interpolation : np.ndarray of float, shape(len(pos_from), len(pos_to))
        The interpolation matrix that maps good signals to the location
        of bad signals.

    References
    ----------
    [1] Perrin, F., Pernier, J., Bertrand, O. and Echallier, JF. (1989).
        Spherical splines for scalp potential and current density mapping.
        Electroencephalography Clinical Neurophysiology, Feb; 72(2):184-7.
    """

    pos_from = pos_from.copy()
    pos_to = pos_to.copy()

    # normalize sensor positions to sphere
    _normalize_vectors(pos_from)
    _normalize_vectors(pos_to)

    # cosine angles between source positions
    cosang_from = pos_from.dot(pos_from.T)
    cosang_to_from = pos_to.dot(pos_from.T)
    G_from = _calc_g(cosang_from)
    G_to_from, H_to_from = (f(cosang_to_from) for f in (_calc_g, _calc_h))

    if alpha is not None:
        G_from.flat[::len(G_from) + 1] += alpha

    C_inv = linalg.pinv(G_from)
    interpolation = G_to_from.dot(C_inv)
    return interpolation


def _do_interp_dots(inst, interpolation, goods_idx, bads_idx):
    """Dot product of channel mapping matrix to channel data
    """
    from ..io.base import _BaseRaw
    from ..epochs import _BaseEpochs
    from ..evoked import Evoked

    if isinstance(inst, _BaseRaw):
        inst._data[bads_idx] = interpolation.dot(inst._data[goods_idx])
    elif isinstance(inst, _BaseEpochs):
        inst._data[:, bads_idx, :] = np.einsum('ij,xjy->xiy', interpolation,
                                               inst._data[:, goods_idx, :])
    elif isinstance(inst, Evoked):
        inst.data[bads_idx] = interpolation.dot(inst.data[goods_idx])
    else:
        raise ValueError('Inputs of type {0} are not supported'
                         .format(type(inst)))


def _interpolate_bads_eeg(inst):
    """Interpolate bad EEG channels

    Operates in place.

    Parameters
    ----------
    inst : mne.io.Raw, mne.Epochs or mne.Evoked
        The data to interpolate. Must be preloaded.
    """
    bads_idx = np.zeros(len(inst.ch_names), dtype=np.bool)
    goods_idx = np.zeros(len(inst.ch_names), dtype=np.bool)

    picks = pick_types(inst.info, meg=False, eeg=True, exclude=[])
    bads_idx[picks] = [inst.ch_names[ch] in inst.info['bads'] for ch in picks]

    if len(picks) == 0 or len(bads_idx) == 0:
        return

    goods_idx[picks] = True
    goods_idx[bads_idx] = False

    pos = inst._get_channel_positions(picks)

    # Make sure only EEG are used
    bads_idx_pos = bads_idx[picks]
    goods_idx_pos = goods_idx[picks]

    pos_good = pos[goods_idx_pos]
    pos_bad = pos[bads_idx_pos]

    # test spherical fit
    radius, center = _fit_sphere(pos_good)
    distance = np.sqrt(np.sum((pos_good - center) ** 2, 1))
    distance = np.mean(distance / radius)
    if np.abs(1. - distance) > 0.1:
        logger.warning('Your spherical fit is poor, interpolation results are '
                       'likely to be inaccurate.')

    logger.info('Computing interpolation matrix from {0} sensor '
                'positions'.format(len(pos_good)))

    interpolation = _make_interpolation_matrix(pos_good, pos_bad)

    logger.info('Interpolating {0} sensors'.format(len(pos_bad)))
    _do_interp_dots(inst, interpolation, goods_idx, bads_idx)


def _interpolate_bads_meg(inst, mode='accurate', verbose=None):
    """Interpolate bad channels from data in good channels.

    Parameters
    ----------
    inst : mne.io.Raw, mne.Epochs or mne.Evoked
        The data to interpolate. Must be preloaded.
    mode : str
        Either `'accurate'` or `'fast'`, determines the quality of the
        Legendre polynomial expansion used for interpolation. `'fast'` should
        be sufficient for most applications.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).
    """
    picks_meg = pick_types(inst.info, meg=True, eeg=False, exclude=[])
    ch_names = [inst.info['ch_names'][p] for p in picks_meg]
    picks_good = pick_types(inst.info, meg=True, eeg=False, exclude='bads')

    # select the bad meg channel to be interpolated
    if len(inst.info['bads']) == 0:
        picks_bad = []
    else:
        picks_bad = pick_channels(ch_names, inst.info['bads'],
                                  exclude=[])

    # return without doing anything if there are no meg channels
    if len(picks_meg) == 0 or len(picks_bad) == 0:
        return

    mapping = _map_meg_channels(inst, picks_good, picks_bad, mode=mode)

    _do_interp_dots(inst, mapping, picks_good, picks_bad)
