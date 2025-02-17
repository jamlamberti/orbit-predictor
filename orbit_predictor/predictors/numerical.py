# MIT License
#
# Copyright (c) 2017 Satellogic SA
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from math import degrees, radians, sqrt, cos, sin
import datetime as dt

import numpy as np
from sgp4.earth_gravity import wgs84

from orbit_predictor.predictors.keplerian import KeplerianPredictor
from orbit_predictor.angles import ta_to_M, M_to_ta
from orbit_predictor.keplerian import coe2rv
from orbit_predictor.utils import njit, raan_from_ltan, float_to_hms


OMEGA = 2 * np.pi / (86400 * 365.2421897)  # rad / s
MU_E = wgs84.mu
R_E_KM = wgs84.radiusearthkm
J2 = wgs84.j2


def sun_sync_plane_constellation(num_satellites, *,
                                 alt_km=None, ecc=None, inc_deg=None, ltan_h=12, date=None):
    """Creates num_satellites in the same Sun-synchronous plane, uniformly spaced.

    Parameters
    ----------
    num_satellites : int
        Number of satellites.
    alt_km : float, optional
        Altitude, in km.
    ecc : float, optional
        Eccentricity.
    inc_deg : float, optional
        Inclination, in degrees.
    ltan_h : int, optional
        Local Time of the Ascending Node, in hours (default to noon).
    date : datetime.date, optional
        Reference date for the orbit, (default to today).

    """
    for ta_deg in np.linspace(0, 360, num_satellites, endpoint=False):
        yield J2Predictor.sun_synchronous(
            alt_km=alt_km, ecc=ecc, inc_deg=inc_deg, ltan_h=ltan_h, date=date, ta_deg=ta_deg
        )


@njit
def pkepler(argp, delta_t_sec, ecc, inc, p, raan, sma, ta):
    """Perturbed Kepler problem (only J2)

    Notes
    -----
    Based on algorithm 64 of Vallado 3rd edition

    """
    # Mean motion
    n = sqrt(MU_E / sma ** 3)

    # Initial mean anomaly
    M_0 = ta_to_M(ta, ecc)

    # Update for perturbations
    delta_raan = (
        - (3 * n * R_E_KM ** 2 * J2) / (2 * p ** 2) *
        cos(inc) * delta_t_sec
    )
    raan = raan + delta_raan

    delta_argp = (
        (3 * n * R_E_KM ** 2 * J2) / (4 * p ** 2) *
        (4 - 5 * sin(inc) ** 2) * delta_t_sec
    )
    argp = argp + delta_argp

    M0_dot = (
        (3 * n * R_E_KM ** 2 * J2) / (4 * p ** 2) *
        (2 - 3 * sin(inc) ** 2) * sqrt(1 - ecc ** 2)
    )
    M_dot = n + M0_dot

    # Propagation
    M = M_0 + M_dot * delta_t_sec

    # New true anomaly
    ta = M_to_ta(M, ecc)

    # Position and velocity vectors
    position_eci, velocity_eci = coe2rv(MU_E, p, ecc, inc, raan, argp, ta)

    return position_eci, velocity_eci


class InvalidOrbitError(Exception):
    pass


class J2Predictor(KeplerianPredictor):
    """Propagator that uses secular variations due to J2.

    """
    @classmethod
    def sun_synchronous(cls, *, alt_km=None, ecc=None, inc_deg=None, ltan_h=12, date=None,
                        ta_deg=0):
        """Creates Sun synchronous predictor instance.

        Parameters
        ----------
        alt_km : float, optional
            Altitude, in km.
        ecc : float, optional
            Eccentricity.
        inc_deg : float, optional
            Inclination, in degrees.
        ltan_h : int, optional
            Local Time of the Ascending Node, in hours (default to noon).
        date : datetime.date, optional
            Reference date for the orbit, (default to today).
        ta_deg : float
            Increment or decrement of true anomaly, will adjust the epoch
            accordingly.

        """
        if date is None:
            date = dt.datetime.today().date()

        try:
            with np.errstate(invalid="raise"):
                if alt_km is not None and ecc is not None:
                    # Normal case, solve for inclination
                    sma = R_E_KM + alt_km
                    inc_deg = degrees(np.arccos(
                        (-2 * sma ** (7 / 2) * OMEGA * (1 - ecc ** 2) ** 2)
                        / (3 * R_E_KM ** 2 * J2 * np.sqrt(MU_E))
                    ))

                elif alt_km is not None and inc_deg is not None:
                    # Not so normal case, solve for eccentricity
                    sma = R_E_KM + alt_km
                    ecc = np.sqrt(
                        1
                        - np.sqrt(
                            (-3 * R_E_KM ** 2 * J2 * np.sqrt(MU_E) * np.cos(radians(inc_deg)))
                            / (2 * OMEGA * sma ** (7 / 2))
                        )
                    )

                elif ecc is not None and inc_deg is not None:
                    # Rare case, solve for altitude
                    sma = (-np.cos(radians(inc_deg)) * (3 * R_E_KM ** 2 * J2 * np.sqrt(MU_E))
                           / (2 * OMEGA * (1 - ecc ** 2) ** 2)) ** (2 / 7)

                else:
                    raise ValueError(
                        "Exactly two of altitude, eccentricity and inclination must be given"
                    )

        except FloatingPointError:
            raise InvalidOrbitError("Cannot find Sun-synchronous orbit with given parameters")

        # TODO: Allow change in time or location
        # Right the epoch is fixed given the LTAN, as well as the sub-satellite point
        epoch = dt.datetime(date.year, date.month, date.day, *float_to_hms(ltan_h))
        raan = raan_from_ltan(epoch, ltan_h)

        return cls(sma, ecc, inc_deg, raan, 0, ta_deg, epoch)

    def _propagate_eci(self, when_utc=None):
        """Return position and velocity in the given date using ECI coordinate system.

        """
        # Orbit parameters
        sma = self._sma
        ecc = self._ecc
        p = sma * (1 - ecc ** 2)
        inc = radians(self._inc)
        raan = radians(self._raan)
        argp = radians(self._argp)
        ta = radians(self._ta)

        delta_t_sec = (when_utc - self._epoch).total_seconds()

        # Propagate
        position_eci, velocity_eci = pkepler(argp, delta_t_sec, ecc, inc, p, raan, sma, ta)

        return tuple(position_eci), tuple(velocity_eci)
