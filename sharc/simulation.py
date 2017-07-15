# -*- coding: utf-8 -*-
"""
Created on Wed Jan 11 19:04:03 2017

@author: edgar
"""

from abc import ABC, abstractmethod
from sharc.support.observable import Observable

import numpy as np
import math
import random
import sys
import matplotlib.pyplot as plt


from sharc.support.enumerations import StationType
from sharc.topology.topology_factory import TopologyFactory
from sharc.propagation.propagation_factory import PropagationFactory
from sharc.parameters.parameters_imt import ParametersImt
from sharc.parameters.parameters_antenna_imt import ParametersAntennaImt
from sharc.parameters.parameters_fss import ParametersFss
from sharc.propagation.propagation import Propagation
from sharc.station_manager import StationManager
from sharc.results import Results
 
class Simulation(ABC, Observable):
    
    def __init__(self, param_imt: ParametersImt, param_system: ParametersFss, param_ant: ParametersAntennaImt):
        ABC.__init__(self)
        Observable.__init__(self)

        self.param_imt = param_imt
        self.param_system = param_system
        self.param_imt_antenna = param_ant

        self.topology = TopologyFactory.createTopology(self.param_imt)

        self.propagation_imt = PropagationFactory.createPropagation(self.param_imt.channel_model)
        self.propagation_system = PropagationFactory.createPropagation(self.param_system.channel_model)

        self.imt_bs_antenna_gain = list()
        self.path_loss_imt = np.empty(0)
        self.coupling_loss_imt = np.empty(0)
        self.coupling_loss_imt_system = np.empty(0)

        self.bs_to_ue_phi = np.empty(0)
        self.bs_to_ue_theta = np.empty(0)
        self.bs_to_ue_beam_rbs = np.empty(0)

        self.ue = np.empty(0)
        self.bs = np.empty(0)
        self.system = np.empty(0)
        
        self.link = dict()

        self.num_rb_per_bs = 0
        self.num_rb_per_ue = 0

        self.results = None          
        
        
    def initialize(self, *args, **kwargs):
        """
        This method is executed only once to initialize the simulation variables. 
        """        
        self.topology.calculate_coordinates()
        num_bs = self.topology.num_base_stations
        num_ue = num_bs*self.param_imt.ue_k*self.param_imt.ue_k_m
        
        self.imt_bs_antenna_gain = list()
        self.path_loss_imt = np.empty([num_bs, num_ue])
        self.coupling_loss_imt = np.empty([num_bs, num_ue])
        self.coupling_loss_imt_system = np.empty(num_ue)

        self.bs_to_ue_phi = np.empty([num_bs, num_ue])
        self.bs_to_ue_theta = np.empty([num_bs, num_ue])
        self.bs_to_ue_beam_rbs = -1.0*np.ones(num_ue, dtype=int)

        self.ue = np.empty(num_ue)
        self.bs = np.empty(num_bs)
        self.system = np.empty(1)

        # this attribute indicates the list of UE's that are connected to each
        # base station. The position the the list indicates the resource block
        # group that is allocated to the given UE
        self.link = dict([(bs,list()) for bs in range(num_bs)])

        # calculates the number of RB per BS
        self.num_rb_per_bs = math.trunc((1-self.param_imt.guard_band_ratio)* \
                            self.param_imt.bandwidth /self.param_imt.rb_bandwidth)
        # calculates the number of RB per UE on a given BS
        self.num_rb_per_ue = math.trunc(self.num_rb_per_bs/self.param_imt.ue_k)
        
        self.results = Results()    
    

    def finalize(self, *args, **kwargs):
        """
        Finalizes the simulation (collect final results, etc...)
        """        
        snapshot_number = kwargs["snapshot_number"]
        self.results.write_files(snapshot_number)    
    
    
    def calculate_coupling_loss(self,
                                station_a: StationManager,
                                station_b: StationManager,
                                propagation: Propagation) -> np.array:
        """
        Calculates the path coupling loss from each station_a to all station_b.
        Result is returned as a numpy array with dimensions num_a x num_b
        TODO: calculate coupling loss between activa stations only
        """
        # Calculate distance from transmitters to receivers. The result is a
        # num_bs x num_ue array
        d_2D = station_a.get_distance_to(station_b)
        d_3D = station_a.get_3d_distance_to(station_b)

        if station_a.station_type is StationType.FSS_SS:
            elevation_angles = station_b.get_elevation_angle(station_a, self.param_system)
            path_loss = propagation.get_loss(distance_3D=d_3D, 
                                             frequency=self.param_system.frequency*np.ones(d_3D.shape),
                                             indoor_stations=np.tile(station_b.indoor, (station_a.num_stations, 1)),
                                             elevation=elevation_angles, 
                                             sat_params = self.param_system,
                                             earth_to_space = True,
                                             line_of_sight_prob=self.param_system.line_of_sight_prob)
        else:
            path_loss = propagation.get_loss(distance_3D=d_3D, 
                                             distance_2D=d_2D, 
                                             frequency=self.param_imt.frequency*np.ones(d_2D.shape),
                                             indoor_stations=np.tile(station_b.indoor, (station_a.num_stations, 1)),
                                             bs_height=station_a.height,
                                             ue_height=station_b.height,
                                             shadowing=self.param_imt.shadowing,
                                             line_of_sight_prob=self.param_imt.line_of_sight_prob)
            self.path_loss_imt = path_loss
        # define antenna gains
        gain_a = self.calculate_gains(station_a, station_b)
        gain_b = np.transpose(self.calculate_gains(station_b, station_a))
        
        # collect IMT BS antenna gain samples
        if station_a.station_type is StationType.IMT_BS:
            self.imt_bs_antenna_gain = gain_a
        
        # calculate coupling loss
        coupling_loss = np.squeeze(path_loss - gain_a - gain_b)
        
        return coupling_loss
    
        
    def connect_ue_to_bs(self):
        """
        Link the UE's to the serving BS. It is assumed that each group of K*M
        user equipments are distributed and pointed to a certain base station
        according to the decisions taken at TG 5/1 meeting
        """
        num_ue_per_bs = self.param_imt.ue_k*self.param_imt.ue_k_m
        bs_active = np.where(self.bs.active)[0]
        for bs in bs_active:
            ue_list = [i for i in range(bs*num_ue_per_bs, bs*num_ue_per_bs + num_ue_per_bs)]
            self.link[bs] = ue_list


    def select_ue(self):
        """
        Select K UEs randomly from all the UEs linked to one BS as “chosen”
        UEs. These K “chosen” UEs will be scheduled during this snapshot.
        """               
        self.bs_to_ue_phi, self.bs_to_ue_theta = \
            self.bs.get_pointing_vector_to(self.ue)
        
        bs_active = np.where(self.bs.active)[0]
        for bs in bs_active:
            # select K UE's among the ones that are connected to BS
            random.shuffle(self.link[bs])
            K = self.param_imt.ue_k
            del self.link[bs][K:]
            # Activate the selected UE's and create beams
            if self.bs.active[bs]:
                self.ue.active[self.link[bs]] = np.ones(K, dtype=bool)
                for ue in self.link[bs]:
                    # add beam to BS antennas
                    self.bs.antenna[bs].add_beam(self.bs_to_ue_phi[bs,ue],
                                             self.bs_to_ue_theta[bs,ue])
                    # add beam to UE antennas
                    self.ue.antenna[ue].add_beam(self.bs_to_ue_phi[bs,ue] - 180,
                                             180 - self.bs_to_ue_theta[bs,ue])
                    # set beam resource block group
                    self.bs_to_ue_beam_rbs[ue] = len(self.bs.antenna[bs].beams_list) - 1

                
    def scheduler(self):
        """
        This scheduler divides the available resource blocks among UE's for
        a given BS
        """
        bs_active = np.where(self.bs.active)[0]
        for bs in bs_active:
            ue_list = self.link[bs]
            self.ue.bandwidth[ue_list] = self.num_rb_per_ue*self.param_imt.rb_bandwidth

        
            
    def calculate_gains(self,
                        station_a: StationManager,
                        station_b: StationManager) -> np.array:
        """
        Calculates the gains of antennas in station_a in the direction of
        station_b        
        """
        phi, theta = station_a.get_pointing_vector_to(station_b)
        
        if(station_a.station_type == StationType.IMT_BS):
            beams_idx = self.bs_to_ue_beam_rbs
        elif(station_a.station_type == StationType.IMT_UE):
            beams_idx = np.zeros(self.bs.num_stations,dtype=int)
        elif(station_a.station_type == StationType.FSS_SS):
            beams_idx = np.zeros(self.ue.num_stations,dtype=int)
        
        gains = np.zeros(phi.shape)
        station_a_active = np.where(station_a.active)[0]
        station_b_active = np.where(station_b.active)[0]
        for k in station_a_active:
            gains[k,station_b_active] = station_a.antenna[k].calculate_gain(phi_vec=phi[k,station_b_active],
                                                                            theta_vec=theta[k,station_b_active],
                                                                            beams_l=beams_idx[station_b_active])
                
        return gains
        
        
    def plot_scenario(self):
        fig = plt.figure(figsize=(8,8), facecolor='w', edgecolor='k')
        ax = fig.gca()
        
        # Plot network topology
        self.topology.plot(ax)
        
        # Plot user equipments
        ax.scatter(self.ue.x, self.ue.y, color='r', edgecolor="w", linewidth=0.5, label="UE")
        
        # Plot UE's azimuth
        d = 0.1 * self.topology.cell_radius
        for i in range(len(self.ue.x)):
            plt.plot([self.ue.x[i], self.ue.x[i] + d*math.cos(math.radians(self.ue.azimuth[i]))], 
                     [self.ue.y[i], self.ue.y[i] + d*math.sin(math.radians(self.ue.azimuth[i]))], 
                     'r-')        
        
        plt.axis('image') 
        plt.title("Simulation scenario")
        plt.xlabel("x-coordinate [m]")
        plt.ylabel("y-coordinate [m]")
        plt.legend(loc="upper left", scatterpoints=1)
        plt.tight_layout()    
        plt.show()        
        
        sys.exit(0)        
        
        
    @abstractmethod
    def snapshot(self, *args, **kwargs):
        """
        Performs a single snapshot.
        """
        pass

        
    @abstractmethod
    def power_control(self):
        """
        Apply downlink power control algorithm
        """    
    
    
    @abstractmethod
    def collect_results(self, *args, **kwargs):
        """
        Collects results. 
        """
        pass