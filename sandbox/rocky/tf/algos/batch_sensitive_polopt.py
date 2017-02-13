import matplotlib
matplotlib.use('Pdf')

import time
from rllab.algos.base import RLAlgorithm
import rllab.misc.logger as logger
import rllab.plotter as plotter
from sandbox.rocky.tf.policies.base import Policy
import tensorflow as tf
from sandbox.rocky.tf.samplers.batch_sampler import BatchSampler
from sandbox.rocky.tf.samplers.vectorized_sampler import VectorizedSampler

import numpy as np


class BatchSensitivePolopt(RLAlgorithm):
    """
    Base class for batch sampling-based policy optimization methods, with sensitive learning.
    This includes various policy gradient methods like vpg, npg, ppo, trpo, etc.
    """

    def __init__(
            self,
            env,
            policy,
            baseline,
            scope=None,
            n_itr=500,
            start_itr=0,
            # Note that the number of trajectories for grad upate = batch_size
            # Defaults are 10 trajectories of length 500 for gradient update
            batch_size=100,
            max_path_length=500,
            meta_batch_size = 100,
            discount=0.99,
            gae_lambda=1,
            plot=False,
            pause_for_plot=False,
            center_adv=True,
            positive_adv=False,
            store_paths=False,
            whole_paths=True,
            fixed_horizon=False,
            sampler_cls=None,
            sampler_args=None,
            force_batch_sampler=False,
            use_sensitive=True,
            **kwargs
    ):
        """
        :param env: Environment
        :param policy: Policy
        :type policy: Policy
        :param baseline: Baseline
        :param scope: Scope for identifying the algorithm. Must be specified if running multiple algorithms
        simultaneously, each using different environments and policies
        :param n_itr: Number of iterations.
        :param start_itr: Starting iteration.
        :param batch_size: Number of samples per iteration.  #
        :param max_path_length: Maximum length of a single rollout.
        :param discount: Discount.
        :param gae_lambda: Lambda used for generalized advantage estimation.
        :param plot: Plot evaluation run after each iteration.
        :param pause_for_plot: Whether to pause before contiuing when plotting.
        :param center_adv: Whether to rescale the advantages so that they have mean 0 and standard deviation 1.
        :param positive_adv: Whether to shift the advantages so that they are always positive. When used in
        conjunction with center_adv the advantages will be standardized before shifting.
        :param store_paths: Whether to save all paths data to the snapshot.
        :return:
        """
        self.env = env
        self.policy = policy
        self.baseline = baseline
        self.scope = scope
        self.n_itr = n_itr
        self.start_itr = start_itr
        # self.batch_size is the number of total transitions to collect.
        # batch_size is the number of trajectories for one fast grad update.
        self.batch_size = batch_size * max_path_length * meta_batch_size
        self.max_path_length = max_path_length
        self.discount = discount
        self.gae_lambda = gae_lambda
        self.plot = plot
        self.pause_for_plot = pause_for_plot
        self.center_adv = center_adv
        self.positive_adv = positive_adv
        self.store_paths = store_paths
        self.whole_paths = whole_paths
        self.fixed_horizon = fixed_horizon
        self.meta_batch_size = meta_batch_size # number of tasks

        if sampler_cls is None:
            if self.policy.vectorized and not force_batch_sampler:
                sampler_cls = VectorizedSampler
            else:
                raise NotImplementedError('need # of envs')
                sampler_cls = BatchSampler
        if sampler_args is None:
            sampler_args = dict()
        sampler_args['n_envs'] = self.meta_batch_size
        self.sampler = sampler_cls(self, **sampler_args)
        self.init_opt()

    def start_worker(self):
        self.sampler.start_worker()
        if self.plot:
            plotter.init_plot(self.env, self.policy)

    def shutdown_worker(self):
        self.sampler.shutdown_worker()

    def obtain_samples(self, itr, reset_args=None):
        # This obtains samples using self.policy, and calling policy.get_actions(obses)
        paths = self.sampler.obtain_samples(itr, reset_args, return_dict=True)
        assert type(paths) == dict
        return paths

    def process_samples(self, itr, paths, prefix='', log=True):
        return self.sampler.process_samples(itr, paths, prefix=prefix, log=log)

    def train(self):
        # TODO - make this a util
        flatten_list = lambda l: [item for sublist in l for item in sublist]

        with tf.Session() as sess:
            sess.run(tf.initialize_all_variables())
            self.start_worker()
            start_time = time.time()
            for itr in range(self.start_itr, self.n_itr):
                itr_start_time = time.time()
                with logger.prefix('itr #%d | ' % itr):
                    # TODO - this is specific to the pointmass task / goal task.
                    # point mass:
                    if self.env.observation_space.shape[0] <= 4:  # pointmass (oracle=4, normal=2)
                        learner_env_goals = np.zeros((self.meta_batch_size, 2, ))
                        """
                        # 0d
                        goals = [np.array([-0.5,0]), np.array([0.5,0])]
                        goals = [np.array([0.5,0.1]), np.array([0.5,-0.1])]
                        goals = [np.array([0.5,0.0]), np.array([-0.5,0.0]),
                                 np.array([0.0,0.5]), np.array([0.0,-0.5]),
                                 np.array([0.5,0.5]), np.array([0.5,-0.5]),
                                 np.array([-0.5,0.5]), np.array([-0.5,-0.5]),
                                 ]
                        for i in range(self.meta_batch_size):
                            learner_env_goals[i,:] = goals[np.random.randint(len(goals))]
                        """
                        # 2d
                        learner_env_goals = np.random.uniform(-0.5, 0.5, size=(self.meta_batch_size, 2, ))
                        #learner_env_goals[:, 1] = 0  # this makes it 1d
                    elif self.env.observation_space.shape[0] == 13:  # swimmer
                        #learner_env_goals = np.random.choice((0.1, 0.2), (self.meta_batch_size, ))
                        #learner_env_goals = np.random.uniform(0.1, 0.2, (self.meta_batch_size, ))
                        learner_env_goals = np.random.uniform(0.0, 0.2, (self.meta_batch_size, ))
                    else:
                        raise NotImplementedError('unrecognized env')

                    logger.log("Obtaining samples using the pre-update policy...")
                    self.policy.switch_to_init_dist()  # Switch to pre-update policy
                    preupdate_paths = self.obtain_samples(itr, reset_args=learner_env_goals)
                    logger.log("Processing samples...")
                    init_samples_data = {}
                    for key in preupdate_paths.keys():
                        init_samples_data[key] = self.process_samples(itr, preupdate_paths[key], log=False)
                    # for logging purposes only
                    self.process_samples(itr, flatten_list(preupdate_paths.values()), prefix='Pre', log=True)
                    logger.log("Logging pre-update diagnostics...")
                    self.log_diagnostics(flatten_list(preupdate_paths.values()), prefix='Pre')

                    logger.log("Computing policy updates...")
                    self.policy.compute_updated_dists(init_samples_data)

                    logger.log("Obtaining samples using the post-update policies...")
                    postupdate_paths = self.obtain_samples(itr, reset_args=learner_env_goals)
                    logger.log("Processing samples...")
                    updated_samples_data = {}
                    for key in postupdate_paths.keys():
                        updated_samples_data[key] = self.process_samples(itr, postupdate_paths[key], log=False)
                    # for logging purposes only
                    self.process_samples(itr, flatten_list(postupdate_paths.values()), prefix='Post1', log=True)
                    logger.log("Logging post-update diagnostics...")
                    self.log_diagnostics(flatten_list(postupdate_paths.values()), prefix='Post1')

                    if itr % 20 == 0:
                        logger.log('Testing policy with multiple grad steps')
                        new_samples_data = updated_samples_data
                        for test_i in range(3):
                            self.policy.compute_updated_dists(new_samples_data)
                            new_paths = self.obtain_samples(itr, reset_args=learner_env_goals)
                            new_samples_data = {}
                            for key in new_paths.keys():
                                new_samples_data[key] = self.process_samples(itr, new_paths[key], log=False)
                                # for logging purposes only
                            self.process_samples(itr, flatten_list(new_paths.values()), prefix='Post'+str(test_i+2), log=True)

                    logger.log("Optimizing policy...")
                    # This needs to take both init_samples_data and samples_data
                    self.optimize_policy(itr, init_samples_data, updated_samples_data)
                    logger.log("Saving snapshot...")
                    params = self.get_itr_snapshot(itr, updated_samples_data)  # , **kwargs)
                    if self.store_paths:
                        params["paths"] = updated_samples_data["paths"]
                    logger.save_itr_params(itr, params)
                    logger.log("Saved")
                    logger.record_tabular('Time', time.time() - start_time)
                    logger.record_tabular('ItrTime', time.time() - itr_start_time)
                    #if self.plot and itr % 2 == 0:
                    if itr % 2 == 0 and self.env.observation_space.shape[0] <= 4: # point-mass
                        logger.log("Saving visualization of paths")
                        import matplotlib.pyplot as plt;
                        for ind in range(5):
                            plt.clf()
                            plt.plot(learner_env_goals[ind][0], learner_env_goals[ind][1], 'k*', markersize=10)
                            plt.hold(True)

                            pre_points = preupdate_paths[ind][0]['observations']
                            post_points = postupdate_paths[ind][0]['observations']
                            plt.plot(pre_points[:,0], pre_points[:,1], '-r', linewidth=2)
                            plt.plot(post_points[:,0], post_points[:,1], '-b', linewidth=1)

                            pre_points = preupdate_paths[ind][1]['observations']
                            post_points = postupdate_paths[ind][1]['observations']
                            plt.plot(pre_points[:,0], pre_points[:,1], '--r', linewidth=2)
                            plt.plot(post_points[:,0], post_points[:,1], '--b', linewidth=1)

                            pre_points = preupdate_paths[ind][2]['observations']
                            post_points = postupdate_paths[ind][2]['observations']
                            plt.plot(pre_points[:,0], pre_points[:,1], '-.r', linewidth=2)
                            plt.plot(post_points[:,0], post_points[:,1], '-.b', linewidth=1)

                            plt.plot(0,0, 'k.', markersize=5)
                            plt.xlim([-0.8, 0.8])
                            plt.ylim([-0.8, 0.8])
                            plt.legend(['goal', 'preupdate path', 'postupdate path'])
                            plt.savefig('/home/cfinn/prepost_path'+str(ind)+'.png')
                    elif itr % 2 == 0:  # swimmer
                        logger.log("Saving visualization of paths")
                        import matplotlib.pyplot as plt;
                        for ind in range(5):
                            plt.clf()
                            goal_vel = learner_env_goals[ind]
                            plt.title('Swimmer paths, goal vel='+str(goal_vel))
                            plt.hold(True)

                            prepathobs = preupdate_paths[ind][0]['observations']
                            postpathobs = postupdate_paths[ind][0]['observations']
                            plt.plot(prepathobs[:,0], prepathobs[:,1], '-r', linewidth=2)
                            plt.plot(postpathobs[:,0], postpathobs[:,1], '--b', linewidth=1)
                            plt.plot(prepathobs[-1,0], prepathobs[-1,1], 'r*', markersize=10)
                            plt.plot(postpathobs[-1,0], postpathobs[-1,1], 'b*', markersize=10)
                            plt.xlim([-1.0, 5.0])
                            plt.ylim([-1.0, 1.0])

                            plt.legend(['preupdate path', 'postupdate path'], loc=2)
                            plt.savefig('/home/cfinn/swim1dlarge_prepost_itr'+str(itr)+'_id'+str(ind)+'.pdf')

                    logger.dump_tabular(with_prefix=False)
                    #if self.plot:
                    #    self.update_plot()
                    #    if self.pause_for_plot:
                    #        input("Plotting evaluation run: Press Enter to "
                    #              "continue...")
        self.shutdown_worker()

    def log_diagnostics(self, paths, prefix):
        self.env.log_diagnostics(paths, prefix)
        self.policy.log_diagnostics(paths, prefix)
        self.baseline.log_diagnostics(paths)

    def init_opt(self):
        """
        Initialize the optimization procedure. If using tensorflow, this may
        include declaring all the variables and compiling functions
        """
        raise NotImplementedError

    def get_itr_snapshot(self, itr, samples_data):
        """
        Returns all the data that should be saved in the snapshot for this
        iteration.
        """
        raise NotImplementedError

    def optimize_policy(self, itr, samples_data):
        raise NotImplementedError

    def update_plot(self):
        if self.plot:
            plotter.update_plot(self.policy, self.max_path_length)
