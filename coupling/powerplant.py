#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Feb 12 15:17:46 2018

@author: witte
"""

# %% imports

import pandas as pd
import numpy as np
import json
from scipy import interpolate
import logging

from tespy import nwkr, logger

# %% power plant model class


class model:
    """
    Creates the model for the power plant. Parameters are loaded from
    coupling data object cd.

    Parameters
    ----------
    cd : coupling_data
        Generel data for the interface handling.

    min_well_depth : float
        Depth of the wells.

    num_wells : int
        Number of wells.

    p_max : float
        Maximum pressure limit.

    p_min : float
        Minimum pressure limit.

    Note
    ----
    The depth of the wells along with the number of wells determines the
    dynamic pressure loss in bore hole pipes connecting the power plant with
    the geological storage. The pressure limits are the pressure limits at the
    bottom of the bore holes. These inforamtion are provided in
    the geological storage model control file.

    Example
    -------
    >>> from coupling import cp, pp
    """

    def __init__(self, cd, min_well_depth, num_wells, p_max, p_min):

        # load data.json information into objects dictionary (= attributes of
        # the object)

        # well information
        self.min_well_depth = min_well_depth
        self.num_wells = num_wells

        # pressure limits
        self.p_max = p_max
        self.p_min = p_min

        path = (cd.working_dir + cd.powerplant_path + cd.scenario + '.powerplant_ctrl.json')
        self.wdir = cd.working_dir + cd.powerplant_path
        with open(path) as f:
            self.__dict__.update(json.load(f))

        # setting paths to lookup tables
        self.spline_charge_path = wdir + self.spline_charge_path
        self.spline_discharge_path = wdir + self.spline_discharge_path

        # load tespy models with the network_reader module
        self.tespy_charge = nwkr.load_nwk(wdir + self.tespy_charge_path)
        self.tespy_charge.set_printoptions(print_level='none')
        self.tespy_discharge = nwkr.load_nwk(wdir + self.tespy_discharge_path)
        self.tespy_discharge.set_printoptions(print_level='none')

        self.power_plant_layout()

        # load splines from .csv data
        self.spline_charge = self.load_lookup(self.spline_charge_path)
        self.spline_discharge = self.load_lookup(self.spline_discharge_path)

    def power_plant_layout(self):
        """
        Power plant layout calculation to determine power plant design point using
        nominal power input/output and nominal pressure as inputs.
        """
        # charging
        self.tespy_charge.imp_busses[self.power_bus_charge].set_attr(P=self.power_nominal_charge)
        self.tespy_charge.imp_conns[self.storage_connection_charge].set_attr(p=self.p_nom, m=np.nan)
        self.tespy_charge.imp_comps[self.pipe].set_attr(L=self.min_well_depth)
        self.tespy_charge.solve('design')
        self.tespy_charge.save(self.wdir + 'charge_design')
        m = self.tespy_charge.imp_conns[self.storage_connection_charge].m.val_SI
        msg = 'Nominal mass flow for charging is ' + str(m) + ' at nominal power ' + str(self.power_nominal_charge) + ' and nominal pressure ' + str(self.p_nom) + '.'
        logging.info(msg)

        # discharging
        self.tespy_discharge.imp_busses[self.power_bus_discharge].set_attr(P=self.power_nominal_discharge)
        self.tespy_discharge.imp_conns[self.storage_connection_discharge].set_attr(p=self.p_nom, m=np.nan)
        self.tespy_charge.imp_comps[self.pipe].set_attr(L=self.min_well_depth)
        self.tespy_discharge.solve('design')
        self.tespy_charge.save(self.wdir + 'discharge_design')
        m = self.tespy_discharge.imp_conns[self.storage_connection_discharge].m.val_SI
        msg = 'Nominal mass flow for discharging is ' + str(m) + ' at nominal power ' + str(self.power_nominal_discharge) + ' and nominal pressure ' + str(self.p_nom) + '.'
        logging.info(msg)

    def get_mass_flow(self, power, pressure, mode):
        """
        Calculate the mass flow at given power input (charging) or
        power output (discharging) and pressure at bottom borehole pressure.

        Parameters
        ----------
        power : float
            Scheduled electrical power input/output of the power plant.

        pressure : float
            Bottom borehole pressure.

        mode : str
            Calculation mode: :code:`mode in ['charging', 'discharging']`.

        calculates the mass flow at given power and pressure in charging or
        discharging mode

        Returns
        -------
        mass_flow : float
            Air mass flow from/into the storage.

        power_actual : float
            Actual electrical power input/output of the power plant.
            Differs from scheduled power, if schedule can not be met.
         """
        if pressure + 1e-4 < self.p_min:
            logging.error('Pressure is below minimum pressure: min=' + str(self.p_min) + 'value=' + str(pressure) + '.')
            return 0, 0
        if pressure - 1e-4 > self.p_max:
            logging.error('Pressure is above maximum pressure: max=' + str(self.p_max) + 'value=' + str(pressure) + '.')
            return 0, 0

        if self.method == 'tespy':
            if mode == 'charging':
                # if power too small
                if abs(power) < abs(self.power_nominal_charge / 100):
                    return 0, 0

                design_path = self.wdir + 'charge_design'
                # set power of bus
                self.tespy_charge.imp_busses[self.power_bus_charge].set_attr(P=power)
                # set pressure at interface
                self.tespy_charge.imp_conns[self.storage_connection_charge].set_attr(p=pressure, m=np.nan)

                try:
                    self.tespy_charge.solve(mode='offdesign', design_path=design_path)
                    if self.tespy_charge.res[-1] > 1e-3:
                        logging.error('Could not find a solution for input pair power=' + str(power) + ' pressure=' + str(pressure) + '.')
                        return 0, 0
                    elif self.cas_charge.m.val_SI < self.massflow_min_rel * self.massflow_charge_max:
                        logging.error('Mass flow for input pair power=' + str(power) + ' pressure=' + str(pressure) + ' below minimum mass flow.')
                        return 0, 0
                    elif self.cas_charge.m.val_SI > self.massflow_charge_max:
                        logging.warning('Mass flow for input pair power=' + str(power) + ' pressure=' + str(pressure) + ' above maximum mass flow. Adjusting power to match maximum allowed mass flow.')
                        return self.massflow_charge_max, self.get_power(self.massflow_charge_max, pressure, mode)
                    else:
                        m = self.tespy_charge.imp_conns[self.storage_connection_charge].m.val_SI
                        logging.info('Calculation successful for power=' + str(power) + ' pressure=' + str(pressure) + '. Mass flow=' + str(m) + '.')
                        return m, power
                except:
                    logging.error('Could not find a solution for input pair power=' + str(power) + ' pressure=' + str(pressure) + '.')
                    return 0, 0

            elif mode == 'discharging':
                if abs(power) < abs(self.power_nominal_discharge / 100):
                    return 0, 0

                design_path = self.wdir + 'discharge_design'
                # set power of bus
                self.tespy_discharge.imp_busses[self.power_bus_discharge].set_attr(P=power)
                # set pressure at interface
                self.tespy_discharge.imp_conns[self.storage_connection_discharge].set_attr(p=pressure, m=np.nan)

                try:
                    self.tespy_discharge.solve(mode='offdesign', design_path=design_path)
                    if self.tespy_discharge.res[-1] > 1e-3:
                        print('ERROR: Could not find a solution for input pair: '
                              'power=' + str(power) + ' pressure=' + str(pressure))
                        return 0, 0
                    elif self.cas_discharge.m.val_SI < self.massflow_min_rel * self.massflow_discharge_max:
                        print('ERROR: massflow for input pair '
                              'power=' + str(power) + ' pressure=' + str(pressure) + ' below minimum massflow.')
                        return 0, 0
                    elif self.cas_discharge.m.val_SI > self.massflow_discharge_max:
                        print('ERROR: massflow for input pair '
                              'power=' + str(power) + ' pressure=' + str(pressure) + ' above maximum massflow.')
                        return self.massflow_discharge_max, self.get_power(self.massflow_discharge_max, pressure, mode)
                    else:
                        return self.tespy_discharge.imp_conns[self.storage_connection_discharge].m.val_SI, power
                except:
                    print('ERROR: Could not find a solution for input pair: '
                          'power=' + str(power) + ' pressure=' + str(pressure))
                    return 0, 0

            else:
                raise ValueError('Mode must be charging or discharging.')

        elif self.method == 'spline':
            if mode == 'charging':
                func = self.spline_charge

            elif mode == 'discharging':
                func = self.spline_discharge
                power = -power
            else:
                raise ValueError('Mode must be charging or discharging.')

            mass_flow = newton(reverse_2d, reverse_2d_deriv,
                               [func, pressure, power], 0)

            if mass_flow == 0:
                print('ERROR: Could not find a solution for input pair: '
                      'power=' + str(power) + ' pressure=' + str(pressure))

            return mass_flow

        else:
            raise ValueError('Method must be tespy or spline.')

    def get_power(self, massflow, pressure, mode):
        """
        calculates the power at given mass flow and pressure in charging or
        discharging mode

        :param massflow: massflow from/to cas
        :type massflow: float
        :param pressure: interface pressure
        :type pressure: float
        :param mode: calculate massflow for charging or discharging
        :type mode: str
        :returns: power (float) - total turbine/compressor power
        :raises: - :code:`ValueError`, mode is neither charge nor discharge
                 - :code:`ValueError`, if calculation method is not specified
         """
        if pressure + 1e-4 < self.p_min:
            print('ERROR: Pressure is below minimum pressure: '
                  'min=' + str(self.p_min) + 'value=' + str(pressure))
            return 0
        if pressure - 1e-4 > self.p_max:
            print('ERROR: Pressure is above maximum pressure: '
                  'max=' + str(self.p_max) + 'value=' + str(pressure))
            return 0

        if self.method == 'tespy':
            if mode == 'charging':
                m_min = self.massflow_min_rel * self.massflow_charge_max
                m_max = self.massflow_charge_max
                if massflow < m_min - 1e-4 :
                    print('ERROR: Massflow is below minimum massflow: '
                          'min=' + str(m_min) +
                          'value=' + str(massflow))
                    return 0
                if massflow > m_max + 1e-4:
                    print('ERROR: Massflow is above maximum massflow: '
                          'max=' + str(m_max) +
                          'value=' + str(massflow))
                    return self.get_power(m_max, pressure, mode)

                init_file = self.tespy_charge_path + '/results.csv'
                self.tespy_charge.busses[0].set_attr(P=np.nan)
                # set mass flow and pressure at interface
                if hasattr(self, 'cas_charge'):
                    self.cas_charge.set_attr(p=pressure)
                    self.cas_charge.set_attr(m=massflow)
                else:
                    for c in self.tespy_charge.conns.index:
                        if c.t.label == 'cas':
                            self.cas_charge = c
                            self.cas_charge.set_attr(p=pressure)
                            self.cas_charge.set_attr(m=massflow)
                            break

                self.tespy_charge.solve(mode='offdesign',
                                        init_file=init_file,
                                        design_file=init_file)

                return self.tespy_charge.busses[0].P.val

            elif mode == 'discharging':
                m_min = self.massflow_min_rel * self.massflow_discharge_max
                m_max = self.massflow_discharge_max
                if massflow < m_min - 1e-4:
                    print('ERROR: Massflow is below minimum massflow: '
                          'min=' + str(m_min) +
                          'value=' + str(massflow))
                    return 0
                if massflow > m_max + 1e-4:
                    print('ERROR: Massflow is above maximum massflow: '
                          'max=' + str(m_max) +
                          'value=' + str(massflow))
                    return self.get_power(m_max, pressure, mode)

                init_file = self.tespy_discharge_path + '/results.csv'
                self.tespy_discharge.busses[0].set_attr(P=np.nan)
                # set mass flow and pressure at interface
                if hasattr(self, 'cas_discharge'):
                    self.cas_discharge.set_attr(p=pressure)
                    self.cas_discharge.set_attr(m=massflow)
                else:
                    for c in self.tespy_discharge.conns.index:
                        if c.s.label == 'cas':
                            self.cas_discharge = c
                            self.cas_discharge.set_attr(p=pressure)
                            self.cas_discharge.set_attr(m=massflow)
                            break

                self.tespy_discharge.solve(mode='offdesign',
                                           init_file=init_file,
                                           design_file=init_file)

                return self.tespy_discharge.busses[0].P.val

            else:
                raise ValueError('Mode must be charge or discharge.')

        elif self.method == 'spline':
            if mode == 'charging':
                val = self.spline_charge.ev(massflow, pressure)
                if abs((self.get_mass_flow(val, pressure, mode) - massflow) /
                       massflow) < 1e-5:
                    return val
                else:
                    return 0

            elif mode == 'discharging':
                val = self.spline_discharge.ev(massflow, pressure)
                if abs((self.get_mass_flow(val, pressure, mode) - massflow) /
                       massflow) < 1e-5:
                    return val
                else:
                    return 0

            else:
                raise ValueError('Mode must be charge or discharge.')
        else:
            raise ValueError('Method must be tespy or spline.')

    def load_lookup(self, path):
        """
        creates a rectangular bivariate spline object from data given in path

        :param path: path to .csv-file containing LUT's data
        :type path: str
        :returns: func (scipy.interpolate.RectBivariateSpline) - spline
                  interpolation object
         """
        df = pd.read_csv(path, index_col=0)

        y = df.as_matrix()  # power

        x1 = df.index.get_values()  # mass flow
        if x1[0] > x1[-1]:
            x1 = x1[::-1]
            y = y[::-1]
        x2 = np.array(list(map(float, list(df))))  # pressure
        if x2[0] > x2[-1]:
            x2 = x2[::-1]
            y = y[:, ::-1]

        func = interpolate.RectBivariateSpline(x1, x2, y)
        return func

# %% these parts are important for the spline lut!


def reverse_2d(params, y):
    r"""
    reverse function for lookup table

    :param params: variable function parameters
    :type params: list
    :param y: functional value, so that :math:`x_2 -
              f\left(x_1, y \right) = 0`
    :type y: float
    :returns: residual value of the function :math:`x_2 -
              f\left(x_1, y \right)`
    """
    func, x1, x2 = params[0], params[1], params[2]
    return x2 - func.ev(y, x1)


def reverse_2d_deriv(params, y):
    r"""
    derivative of the reverse function for a lookup table

    :param params: variable function parameters
    :type params: list
    :param y: functional value, so that :math:`x_2 -
              f\left(x_1, y \right) = 0`
    :type y: float
    :returns: partial derivative :math:`\frac{\partial f}{\partial y}`
    """
    func, x1 = params[0], params[1]
    return - func.ev(y, x1, dx=1)


def newton(func, deriv, params, k, **kwargs):
    r"""
    find zero crossings of function func with 1-D newton algorithm,
    required for reverse functions of fluid mixtures

    :param func: function to find zero crossing in
    :type func: function
    :param deriv: derivative of the function
    :type deriv: function
    :param params: vector containing parameters for func
    :type params: list
    :param k: target value for function func
    :type k: numeric
    :returns: val (float) - val, so that func(params, val) = k

    **allowed keywords** in kwargs:

    - val0 (*numeric*) - starting value
    - valmin (*numeric*) - minimum value
    - valmax (*numeric*) - maximum value
    - imax (*numeric*) - maximum number of iterations

    .. math::

        x_{i+1} = x_{i} - \frac{f(x_{i})}{\frac{df}{dx}(x_{i})}\\
        f(x_{n}) \leq \epsilon, \; n < 10\\
        n: \text{number of iterations}
    """

    # default valaues
    val = kwargs.get('val0', 200)
    valmin = kwargs.get('valmin', 0)
    valmax = kwargs.get('valmax', 3000)
    imax = kwargs.get('imax', 10)

    # start newton loop
    res = 1
    i = 0
    while abs(res) >= 1e-5:
        # calculate function residual
        res = k - func(params, val)
        # calculate new value
        val += res / deriv(params, val)

        # check for value ranges
        if val < valmin:
            val = valmin
        if val > valmax:
            val = valmax
        i += 1

        if i > imax:
            return 0

    return val
