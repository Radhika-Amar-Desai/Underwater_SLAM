import gtsam
from functools import partial
import matplotlib.pyplot as plt
import numpy as np
from gtsam import NavState, Point3, Pose3, Rot3
from gtsam.symbol_shorthand import B, V, X
from mpl_toolkits.mplot3d import Axes3D
from scipy.spatial.transform import Rotation as R
from typing import Optional, List

from dataloader import *

BIAS_KEY = B(0)


class AUViSAM:
    def __init__(self):
        '''
        The nodes on the graph will be gtsam.NavState, which is essentially a
        SE_2(3) lie group representation of the state of the vehicle.

        For this script, will be testing out the use of the IMU, depth sensor, 
        odometry, and velocity logger.
        '''

        # Initialization of some parameters
        self.dt = 1e-6
        self.priorNoise = gtsam.noiseModel.Isotropic.Sigma(6, 0.1)
        self.velNoise = gtsam.noiseModel.Isotropic.Sigma(3, 0.1)

        # IMU shiz
        acc_bias = np.array([0.067, 0.115, 0.320])
        gyro_bias = np.array([0.067, 0.115, 0.320])
        bias = gtsam.imuBias.ConstantBias(acc_bias, gyro_bias)
        self.params = gtsam.PreintegrationParams.MakeSharedU(9.81)
        self.pim = gtsam.PreintegratedImuMeasurements(
            self.params, bias)

        # Load data
        self.iekf_states = read_iekf_states('states.csv')
        self.iekf_times = read_state_times('state_times.csv')
        self.imu_times, self.imu = read_imu('full_dataset/imu_adis_ros.csv')
        self.depth_times, self.depth = read_depth_sensor(
            'full_dataset/depth_sensor.csv')

    def get_nav_state(self, time):
        '''
        Get the state from the Invariant EKF at time "time" and store
        as a gtsam.NavState to initialize values and/or set nodes in the graph

        Inputs
        =======
        time: int
            Index of the time in the time vector

        Returns
        =======
        nav_state: gtsam.NavState
            The state at time "time"
        '''
        x = self.iekf_states['x'][time]
        y = self.iekf_states['y'][time]
        z = self.iekf_states['z'][time]
        u = self.iekf_states['u'][time]
        v = self.iekf_states['v'][time]
        r = self.iekf_states['r'][time]
        phi = self.iekf_states['phi'][time]
        theta = self.iekf_states['theta'][time]
        psi = self.iekf_states['psi'][time]

        # Think this is the correct way to do it
        # TODO: is this the correct way to do it?
        r1 = R.from_rotvec([phi, 0, 0])
        r2 = R.from_rotvec([0, theta, 0])
        r3 = R.from_rotvec([0, 0, psi])
        rot_mat = r1.as_matrix() @ r2.as_matrix() @ r3.as_matrix()

        p = gtsam.Point3(x, y, z)
        v = gtsam.Point3(u, v, r)

        pose = Pose3(Rot3(rot_mat), p)
        state = NavState(pose, v)
        return state

    def depth_error(self, 
                measurement: np.ndarray, 
                this: gtsam.CustomFactor, 
                values: gtsam.Values, 
                jacobians: Optional[List[np.ndarray]]) -> float:
        '''
        Calculate the error between the odometry measurement and the odometry 
        prediction
        '''
        key = this.keys()[0]
        estimate = values.Pose3(key)
        error = measurement - estimate.z()
        if jacobians is not None:
            val = np.ones((1, 6))
            val[0, 2] = 1
            jacobians[0] = val
        return error

    def iSAM(self):
        '''
        Optimize over the graph after each new observation is taken

        TODO: Test with only a few states at first
        '''
        isam = gtsam.ISAM2()

        state_idx = 0
        depth_idx = 0
        imu_idx = 0

        time_elapsed = 0
        imu_time_elapsed = 0

        graph = gtsam.NonlinearFactorGraph()
        initial = gtsam.Values()
        while state_idx < 10:
            state = self.get_nav_state(state_idx)
            if state_idx == 0:
                # Add prior to the graph
                priorPoseFactor = gtsam.PriorFactorPose3(
                    X(state_idx), state.pose(), self.priorNoise)
                graph.add(priorPoseFactor)

                priorVelFactor = gtsam.PriorFactorPoint3(
                    V(state_idx), state.velocity(), self.velNoise)
                graph.add(priorVelFactor)

                # Add values
                initial.insert(X(state_idx), state.pose())
                initial.insert(V(state_idx), state.velocity())

                # IMU information
                acc_bias = np.array([0.067, 0.115, 0.320])
                gyro_bias = np.array([0.067, 0.115, 0.320])
                bias = gtsam.imuBias.ConstantBias(acc_bias, gyro_bias)
                initial.insert(BIAS_KEY, bias)
            else:
                # Compute time difference between states
                dt = self.iekf_times[state_idx] - \
                    self.iekf_times[state_idx - 1]
                dt *= 1e-9
                if dt <= 0:
                    state_idx += 1
                    continue

                # prev_state = self.get_nav_state(state_idx - 1)
                # initial.insert(X(state_idx), prev_state.pose())
                # initial.insert(V(state_idx), prev_state.velocity())

                # Find the lower time between depth, state and IMU
                sensor_time = -1
                imu_time = self.imu_times[imu_idx]
                depth_time = self.depth_times[depth_idx]
                if imu_time < depth_time:
                    sensor_time = imu_time * 1e-18
                    sensor_type = 'imu'
                else:
                    sensor_time = depth_time * 1e-18
                    sensor_type = 'depth'
                state_time = self.iekf_times[state_idx] * 1e-18
                print(f'State time: {state_time}')

                while state_time > sensor_time:
                    if sensor_type == 'imu':
                        omega_x = self.imu['omega_x'][imu_idx]
                        omega_y = self.imu['omega_y'][imu_idx]
                        omega_z = self.imu['omega_z'][imu_idx]
                        lin_acc_x = self.imu['ax'][imu_idx]
                        lin_acc_y = self.imu['ay'][imu_idx]
                        lin_acc_z = self.imu['az'][imu_idx]

                        measuredOmega = np.array(
                            [omega_x, omega_y, omega_z]).reshape(-1, 1)
                        measuredAcc = np.array(
                            [lin_acc_x, lin_acc_y, lin_acc_z]).reshape(-1, 1)

                        print(f'\tIMU time: {self.imu_times[imu_idx]*1e-18}')

                        if imu_idx > 0:
                            imu_dt = self.imu_times[imu_idx] - \
                                self.imu_times[imu_idx - 1]
                            imu_dt *= 1e-9
                        else:
                            imu_dt = self.iekf_times[state_idx] - \
                                self.iekf_times[state_idx - 1]

                        self.pim.integrateMeasurement(
                            measuredOmega, measuredAcc, imu_dt)

                        imu_idx += 1
                    elif sensor_type == 'depth':
                        print(
                            f'\tDepth time: {self.depth_times[depth_idx]*1e-18}')
                        depth = self.depth[depth_idx]
                        depth_model = gtsam.noiseModel.Isotropic.Sigma(1, 0.01)
                        depth_factor = gtsam.CustomFactor(
                            depth_model, [X(depth_idx)], partial(self.depth_error, np.array([-1 * depth])))
                        print(depth_idx)
                        imu_factor = gtsam.ImuFactor(X(depth_idx), V(depth_idx), X(depth_idx + 1), V(depth_idx + 1), BIAS_KEY, self.pim)

                        graph.add(depth_factor)
                        graph.add(imu_factor)

                        initial.insert(X(depth_idx+1), state.pose())
                        initial.insert(V(depth_idx+1), state.velocity())

                        depth_idx += 1
                        # print(f'\tMeasured depth: {depth}')
                        # print()
                    imu_time = self.imu_times[imu_idx]
                    depth_time = self.depth_times[depth_idx]
                    if imu_time < depth_time:
                        sensor_time = imu_time * 1e-18
                        sensor_type = 'imu'
                    else:
                        sensor_time = depth_time * 1e-18
                        sensor_type = 'depth'

            state_idx += 1
        graph.saveGraph('incremental.dot', initial)

def main():
    AUV_SLAM = AUViSAM()
    AUV_SLAM.iSAM()


if __name__ == '__main__':
    main()
