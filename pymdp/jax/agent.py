#!/usr/bin/env python
# -*- coding: utf-8 -*-

""" Agent Class implementation in Jax

__author__: Conor Heins, Dimitrije Markovic, Alexander Tschantz, Daphne Demekas, Brennan Klein

"""

import math as pymath
import jax.numpy as jnp
import jax.tree_util as jtu
from jax import nn, vmap, random
from . import inference, control, learning, utils, maths
from equinox import Module, field, tree_at

from typing import List, Optional
from jaxtyping import Array
from functools import partial

class Agent(Module):
    """ 
    The Agent class, the highest-level API that wraps together processes for action, perception, and learning under active inference.

    The basic usage is as follows:

    >>> my_agent = Agent(A = A, B = C, <more_params>)
    >>> observation = env.step(initial_action)
    >>> qs = my_agent.infer_states(observation)
    >>> q_pi, G = my_agent.infer_policies()
    >>> next_action = my_agent.sample_action()
    >>> next_observation = env.step(next_action)

    This represents one timestep of an active inference process. Wrapping this step in a loop with an ``Env()`` class that returns
    observations and takes actions as inputs, would entail a dynamic agent-environment interaction.
    """

    A: List[Array]
    B: List[Array]
    C: List[Array] 
    D: List[Array]
    E: Array
    # empirical_prior: List
    gamma: Array
    alpha: Array
    qs: Optional[List[Array]]
    q_pi: Optional[List[Array]]

    # parameters used for inductive inference
    inductive_threshold: Array # threshold for inductive inference (the threshold for pruning transitions that are below a certain probability)
    inductive_epsilon: Array # epsilon for inductive inference (trade-off/weight for how much inductive value contributes to EFE of policies)

    H: List[Array] # H vectors (one per hidden state factor) used for inductive inference -- these encode goal states or constraints
    I: List[Array] # I matrices (one per hidden state factor) used for inductive inference -- these encode the 'reachability' matrices of goal states encoded in `self.H`

    pA: List[Array]
    pB: List[Array]
    
    # static parameters not leaves of the PyTree
    A_dependencies: Optional[List] = field(static=True)
    B_dependencies: Optional[List] = field(static=True)
    batch_size: int = field(static=True)
    num_iter: int = field(static=True)
    num_obs: List[int] = field(static=True)
    num_modalities: int = field(static=True)
    num_states: List[int] = field(static=True)
    num_factors: int = field(static=True)
    num_controls: List[int] = field(static=True)
    control_fac_idx: Optional[List[int]] = field(static=True)
    policy_len: int = field(static=True) # depth of planning during roll-outs (i.e. number of timesteps to look ahead when computing expected free energy of policies)
    inductive_depth: int = field(static=True) # depth of inductive inference (i.e. number of future timesteps to use when computing inductive `I` matrix)
    policies: Array = field(static=True)  # matrix of all possible policies (each row is a policy of shape (num_controls[0], num_controls[1], ..., num_controls[num_control_factors-1])
    use_utility: bool = field(static=True) # flag for whether to use expected utility ("reward" or "preference satisfaction") when computing expected free energy
    use_states_info_gain: bool = field(static=True) # flag for whether to use state information gain ("salience") when computing expected free energy
    use_param_info_gain: bool = field(static=True)  # flag for whether to use parameter information gain ("novelty") when computing expected free energy
    use_inductive: bool = field(static=True)   # flag for whether to use inductive inference ("intentional inference") when computing expected free energy
    onehot_obs: bool = field(static=True)
    action_selection: str = field(static=True) # determinstic or stochastic action selection 
    sampling_mode : str = field(static=True) # whether to sample from full posterior over policies ("full") or from marginal posterior over actions ("marginal")
    inference_algo: str = field(static=True) # fpi, vmp, mmp, ovf

    learn_A: bool = field(static=True)
    learn_B: bool = field(static=True)
    learn_C: bool = field(static=True)
    learn_D: bool = field(static=True)
    learn_E: bool = field(static=True)

    def __init__(
        self,
        A,
        B,
        C,
        D,
        E,
        pA,
        pB,
        A_dependencies=None,
        B_dependencies=None,
        qs=None,
        q_pi=None,
        H=None,
        I=None,
        policy_len=1,
        control_fac_idx=None,
        policies=None,
        gamma=16.0,
        alpha=16.0,
        inductive_depth=1,
        inductive_threshold=0.1,
        inductive_epsilon=1e-3,
        use_utility=True,
        use_states_info_gain=True,
        use_param_info_gain=False,
        use_inductive=False,
        onehot_obs=False,
        action_selection="deterministic",
        sampling_mode="marginal",
        inference_algo="fpi",
        num_iter=16,
        learn_A=True,
        learn_B=True,
        learn_C=False,
        learn_D=True,
        learn_E=False
    ):
        ### PyTree leaves
        self.A = A
        self.B = B
        self.C = C
        self.D = D
        # self.empirical_prior = D
        self.H = H
        self.pA = pA
        self.pB = pB
        self.qs = qs
        self.q_pi = q_pi

        self.onehot_obs = onehot_obs

        element_size = lambda x: x.shape[1]
        self.num_factors = len(self.B)
        self.num_states = jtu.tree_map(element_size, self.B) 

        self.num_modalities = len(self.A)
        self.num_obs = jtu.tree_map(element_size, self.A)

        # Ensure consistency of A_dependencies with num_states and num_factors
        if A_dependencies is not None:
            self.A_dependencies = A_dependencies
        else:
            # assume full dependence of A matrices and state factors
            self.A_dependencies = [list(range(self.num_factors)) for _ in range(self.num_modalities)]
        
        for m in range(self.num_modalities):
            factor_dims = tuple([self.num_states[f] for f in self.A_dependencies[m]])
            assert self.A[m].shape[2:] == factor_dims, f"Please input an `A_dependencies` whose {m}-th indices correspond to the hidden state factors that line up with lagging dimensions of A[{m}]..." 
            if self.pA != None:
                assert self.pA[m].shape[2:] == factor_dims, f"Please input an `A_dependencies` whose {m}-th indices correspond to the hidden state factors that line up with lagging dimensions of pA[{m}]..." 
            assert max(self.A_dependencies[m]) <= (self.num_factors - 1), f"Check modality {m} of `A_dependencies` - must be consistent with `num_states` and `num_factors`..."
           
        # Ensure consistency of B_dependencies with num_states and num_factors
        if B_dependencies is not None:
            self.B_dependencies = B_dependencies
        else:
            self.B_dependencies = [[f] for f in range(self.num_factors)] # defaults to having all factors depend only on themselves

        for f in range(self.num_factors):
            factor_dims = tuple([self.num_states[f] for f in self.B_dependencies[f]])
            assert self.B[f].shape[2:-1] == factor_dims, f"Please input a `B_dependencies` whose {f}-th indices pick out the hidden state factors that line up with the all-but-final lagging dimensions of B[{f}]..." 
            if self.pB != None:
                assert self.pB[f].shape[2:-1] == factor_dims, f"Please input a `B_dependencies` whose {f}-th indices pick out the hidden state factors that line up with the all-but-final lagging dimensions of pB[{f}]..." 
            assert max(self.B_dependencies[f]) <= (self.num_factors - 1), f"Check factor {f} of `B_dependencies` - must be consistent with `num_states` and `num_factors`..."

        self.batch_size = self.A[0].shape[0]

        self.gamma = jnp.broadcast_to(gamma, (self.batch_size,))
        self.alpha = jnp.broadcast_to(alpha, (self.batch_size,))
        self.inductive_threshold = jnp.broadcast_to(inductive_threshold, (self.batch_size,))
        self.inductive_epsilon = jnp.broadcast_to(inductive_epsilon, (self.batch_size,))

        ### Static parameters ###
        self.num_iter = num_iter
        self.inference_algo = inference_algo
        self.inductive_depth = inductive_depth

        # policy parameters
        self.policy_len = policy_len
        self.action_selection = action_selection
        self.sampling_mode = sampling_mode
        self.use_utility = use_utility
        self.use_states_info_gain = use_states_info_gain
        self.use_param_info_gain = use_param_info_gain
        self.use_inductive = use_inductive

        if self.use_inductive and self.H is not None:
            # print("Using inductive inference...")
            self.I = self._construct_I()
        elif self.use_inductive and I is not None:
            self.I = I
        else:
            self.I = jtu.tree_map(lambda x: jnp.expand_dims(jnp.zeros_like(x), 1), self.D)

        # learning parameters
        self.learn_A = learn_A
        self.learn_B = learn_B
        self.learn_C = learn_C
        self.learn_D = learn_D
        self.learn_E = learn_E

        """ Determine number of observation modalities and their respective dimensions """
        self.num_obs = [self.A[m].shape[1] for m in range(len(self.A))]
        self.num_modalities = len(self.num_obs)

        # If no `num_controls` are given, then this is inferred from the shapes of the input B matrices
        self.num_controls = [self.B[f].shape[-1] for f in range(self.num_factors)]

        # Users have the option to make only certain factors controllable.
        # default behaviour is to make all hidden state factors controllable
        # (i.e. self.num_states == self.num_controls)
        # Users have the option to make only certain factors controllable.
        # default behaviour is to make all hidden state factors controllable, i.e. `self.num_factors == len(self.num_controls)`
        if control_fac_idx == None:
            self.control_fac_idx = [f for f in range(self.num_factors) if self.num_controls[f] > 1]
        else:
            assert max(control_fac_idx) <= (self.num_factors - 1), "Check control_fac_idx - must be consistent with `num_states` and `num_factors`..."
            self.control_fac_idx = control_fac_idx

            for factor_idx in self.control_fac_idx:
                assert self.num_controls[factor_idx] > 1, "Control factor (and B matrix) dimensions are not consistent with user-given control_fac_idx"

        if policies is not None:
            self.policies = policies
        else:
            self._construct_policies()
        
        # set E to uniform/uninformative prior over policies if not given
        if E is None:
            self.E = jnp.ones((self.batch_size, len(self.policies)))/ len(self.policies)
        else:
            self.E = E

    def _construct_policies(self):
        
        self.policies =  control.construct_policies(
            self.num_states, self.num_controls, self.policy_len, self.control_fac_idx
        )

    @vmap
    def _construct_I(self):
        return control.generate_I_matrix(self.H, self.B, self.inductive_threshold, self.inductive_depth)

    @property
    def unique_multiactions(self):
        size = pymath.prod(self.num_controls)
        return jnp.unique(self.policies[:, 0], axis=0, size=size, fill_value=-1)

    @vmap
    def learning(self, beliefs_A, outcomes, actions, beliefs_B=None, lr_pA=1., lr_pB=1., **kwargs):
        agent = self
        if self.learn_A:
            o_vec_seq = jtu.tree_map(lambda o, dim: nn.one_hot(o, dim), outcomes, self.num_obs)
            qA = learning.update_obs_likelihood_dirichlet(self.pA, o_vec_seq, beliefs_A, self.A_dependencies, lr=lr_pA)
            E_qA = jtu.tree_map(lambda x: maths.dirichlet_expected_value(x), qA)
            agent = tree_at(lambda x: (x.A, x.pA), agent, (E_qA, qA))
            
        if self.learn_B:
            beliefs_B = beliefs_A if beliefs_B is None else beliefs_B
            actions_seq = [actions[..., i] for i in range(actions.shape[-1])] # as many elements as there are control factors, where each element is a jnp.ndarray of shape (n_timesteps, )
            assert beliefs_B[0].shape[0] == actions_seq[0].shape[0] + 1
            actions_onehot = jtu.tree_map(lambda a, dim: nn.one_hot(a, dim, axis=-1), actions_seq, self.num_controls)
            qB = learning.update_state_likelihood_dirichlet(self.pB, beliefs_B, actions_onehot, self.B_dependencies, lr=lr_pB)
            E_qB = jtu.tree_map(lambda x: maths.dirichlet_expected_value(x), qB)

            # if you have updated your beliefs about transitions, you need to re-compute the I matrix used for inductive inferenece
            if self.use_inductive and self.H is not None:
                I_updated = control.generate_I_matrix(self.H, E_qB, self.inductive_threshold, self.inductive_depth)
            else:
                I_updated = self.I

            agent = tree_at(lambda x: (x.B, x.pB, x.I), agent, (E_qB, qB, I_updated))
 
        # if self.learn_C:
        #     self.qC = learning.update_C(self.C, *args, **kwargs)
        #     self.C = jtu.tree_map(lambda x: maths.dirichlet_expected_value(x), self.qC)
        # if self.learn_D:
        #     self.qD = learning.update_D(self.D, *args, **kwargs)
        #     self.D = jtu.tree_map(lambda x: maths.dirichlet_expected_value(x), self.qD)
        # if self.learn_E:
        #     self.qE = learning.update_E(self.E, *args, **kwargs)
        #     self.E = maths.dirichlet_expected_value(self.qE)

        # do stuff
        # variables = ...
        # parameters = ...
        # varibles = {'A': jnp.ones(5)}

        # agent = tree_at(lambda x: (x.A, x.pA, x.B, x.pB, x.I), self, (E_qA, qA, E_qB, qB, I_updated))

        return agent
    
    @vmap
    def infer_states(self, observations, past_actions, empirical_prior, qs_hist, mask=None):
        """
        Update approximate posterior over hidden states by solving variational inference problem, given an observation.

        Parameters
        ----------
        observations: ``list`` or ``tuple`` of ints
            The observation input. Each entry ``observation[m]`` stores one-hot vectors representing the observations for modality ``m``.
        past_actions: ``list`` or ``tuple`` of ints
            The action input. Each entry ``past_actions[f]`` stores indices (or one-hots?) representing the actions for control factor ``f``.
        empirical_prior: ``list`` or ``tuple`` of ``jax.numpy.ndarray`` of dtype object
            Empirical prior beliefs over hidden states. Depending on the inference algorithm chosen, the resulting ``empirical_prior`` variable may be a matrix (or list of matrices) 
            of additional dimensions to encode extra conditioning variables like timepoint and policy.
        Returns
        ---------
        qs: ``numpy.ndarray`` of dtype object
            Posterior beliefs over hidden states. Depending on the inference algorithm chosen, the resulting ``qs`` variable will have additional sub-structure to reflect whether
            beliefs are additionally conditioned on timepoint and policy.
            For example, in case the ``self.inference_algo == 'MMP' `` indexing structure is policy->timepoint-->factor, so that 
            ``qs[p_idx][t_idx][f_idx]`` refers to beliefs about marginal factor ``f_idx`` expected under policy ``p_idx`` 
            at timepoint ``t_idx``.
        """
        if not self.onehot_obs:
            o_vec = [nn.one_hot(o, self.num_obs[m]) for m, o in enumerate(observations)]
        else:
            o_vec = observations
        
        A = self.A
        if mask is not None:
            for i, m in enumerate(mask):
                o_vec[i] = m * o_vec[i] + (1 - m) * jnp.ones_like(o_vec[i]) / self.num_obs[i]
                A[i] = m * A[i] + (1 - m) * jnp.ones_like(A[i]) / self.num_obs[i]
        
        output = inference.update_posterior_states(
            A,
            self.B,
            o_vec,
            past_actions,
            prior=empirical_prior,
            qs_hist=qs_hist,
            A_dependencies=self.A_dependencies,
            B_dependencies=self.B_dependencies,
            num_iter=self.num_iter,
            method=self.inference_algo
        )

        return output

    @partial(vmap, in_axes=(0, 0, 0))
    def update_empirical_prior(self, action, qs):
        # return empirical_prior, and the history of posterior beliefs (filtering distributions) held about hidden states at times 1, 2 ... t

        qs_last = jtu.tree_map( lambda x: x[-1], qs)
        # this computation of the predictive prior is correct only for fully factorised Bs.
        pred = control.compute_expected_state(qs_last, self.B, action, B_dependencies=self.B_dependencies)
        
        return (pred, qs)

    @vmap
    def infer_policies(self, qs: List):
        """
        Perform policy inference by optimizing a posterior (categorical) distribution over policies.
        This distribution is computed as the softmax of ``G * gamma + lnE`` where ``G`` is the negative expected
        free energy of policies, ``gamma`` is a policy precision and ``lnE`` is the (log) prior probability of policies.
        This function returns the posterior over policies as well as the negative expected free energy of each policy.

        Returns
        ----------
        q_pi: 1D ``numpy.ndarray``
            Posterior beliefs over policies, i.e. a vector containing one posterior probability per policy.
        G: 1D ``numpy.ndarray``
            Negative expected free energies of each policy, i.e. a vector containing one negative expected free energy per policy.
        """

        latest_belief = jtu.tree_map(lambda x: x[-1], qs) # only get the posterior belief held at the current timepoint
        q_pi, G = control.update_posterior_policies_inductive(
            self.policies,
            latest_belief, 
            self.A,
            self.B,
            self.C,
            self.E,
            self.pA,
            self.pB,
            A_dependencies=self.A_dependencies,
            B_dependencies=self.B_dependencies,
            I = self.I,
            gamma=self.gamma,
            inductive_epsilon=self.inductive_epsilon,
            use_utility=self.use_utility,
            use_states_info_gain=self.use_states_info_gain,
            use_param_info_gain=self.use_param_info_gain,
            use_inductive=self.use_inductive
        )

        return q_pi, G
    
    @vmap
    def multiaction_probabilities(self, q_pi: Array):
        """
        Compute probabilities of unique multi-actions from the posterior over policies.

        Parameters
        ----------
        q_pi: 1D ``numpy.ndarray``
        Posterior beliefs over policies, i.e. a vector containing one posterior probability per policy.
        
        Returns
        ----------
        multi-action: 1D ``jax.numpy.ndarray``
            Vector containing probabilities of possible multi-actions for different factors
        """

        if self.sampling_mode == "marginal":
            marginals = control.get_marginals(q_pi, self.policies, self.num_controls)
            outer = lambda a, b: jnp.outer(a, b).reshape(-1)
            marginals = jtu.tree_reduce(outer, marginals)

        elif self.sampling_mode == "full":
            locs = jnp.all(
                self.policies[:, 0] == jnp.expand_dims(self.unique_multiactions, -2),
                  -1
            )
            marginals = jnp.where(locs, q_pi, 0.).sum(-1)

        # assert jnp.isclose(jnp.sum(marginals), 1.)  # this fails inside scan           
        return marginals

    @vmap
    def sample_action(self, q_pi: Array, rng_key=None):
        """
        Sample or select a discrete action from the posterior over control states.
        
        Returns
        ----------
        action: 1D ``jax.numpy.ndarray``
            Vector containing the indices of the actions for each control factor
        action_probs: 2D ``jax.numpy.ndarray``
            Array of action probabilities
        """

        if (rng_key is None) and (self.action_selection == "stochastic"):
            raise ValueError("Please provide a random number generator key to sample actions stochastically")

        if self.sampling_mode == "marginal":
            action = control.sample_action(q_pi, self.policies, self.num_controls, self.action_selection, self.alpha, rng_key=rng_key)
        elif self.sampling_mode == "full":
            action = control.sample_policy(q_pi, self.policies, self.num_controls, self.action_selection, self.alpha, rng_key=rng_key)

        return action
    
    def _get_default_params(self):
        method = self.inference_algo
        default_params = None
        if method == "VANILLA":
            default_params = {"num_iter": 8, "dF": 1.0, "dF_tol": 0.001}
        elif method == "MMP":
            raise NotImplementedError("MMP is not implemented")
        elif method == "VMP":
            raise NotImplementedError("VMP is not implemented")
        elif method == "BP":
            raise NotImplementedError("BP is not implemented")
        elif method == "EP":
            raise NotImplementedError("EP is not implemented")
        elif method == "CV":
            raise NotImplementedError("CV is not implemented")

        return default_params
    
    @vmap
    def infer_policies_efevisualize_full(self, qs: List):
        """
        Perform policy inference by optimizing a posterior (categorical) distribution over policies.
        This distribution is computed as the softmax of ``G * gamma + lnE`` where ``G`` is the negative expected
        free energy of policies, ``gamma`` is a policy precision and ``lnE`` is the (log) prior probability of policies.
        This function returns the posterior over policies as well as the negative expected free energy of each policy.

        Returns
        ----------
        q_pi: 1D ``numpy.ndarray``
            Posterior beliefs over policies, i.e. a vector containing one posterior probability per policy.
        G: 1D ``numpy.ndarray``
            Negative expected free energies of each policy, i.e. a vector containing one negative expected free energy per policy.
        """
        
        latest_belief = jtu.tree_map(lambda x: x[-1], qs) # only get the posterior belief held at the current timepoint
        
        q_pi, G, PBS, PKLD, PFE, oRisk, PBS_pA, PBS_pB = control.update_posterior_policies_inductive_efev_full(
            self.policies,
            latest_belief, 
            self.A,
            self.B,
            self.C,
            self.E,
            self.pA,
            self.pB,
            A_dependencies=self.A_dependencies,
            B_dependencies=self.B_dependencies,
            I = self.I,
            gamma=self.gamma,
            inductive_epsilon=self.inductive_epsilon,
            use_utility=self.use_utility,
            use_states_info_gain=self.use_states_info_gain,
            use_param_info_gain=self.use_param_info_gain,
            use_inductive=self.use_inductive
        )
        
        return q_pi, G, PBS, PKLD, PFE, oRisk, PBS_pA, PBS_pB
    
    @vmap
    def sample_action_prob_pi(self, q_pi: Array, rng_key=None):
        """
        Sample or select a discrete action from the posterior over control states.
        
        Returns
        ----------
        action: 1D ``jax.numpy.ndarray``
            Vector containing the indices of the actions for each control factor
        action_probs: 2D ``jax.numpy.ndarray``
            Array of action probabilities
        """

        if (rng_key is None) and (self.action_selection == "stochastic"):
            raise ValueError("Please provide a random number generator key to sample actions stochastically")

        if self.sampling_mode == "marginal":
            action = control.sample_action(q_pi, self.policies, self.num_controls, self.action_selection, self.alpha, rng_key=rng_key)
            prob_pi = None #仮置き
        elif self.sampling_mode == "full":
            action, prob_pi = control.sample_policy_prob_pi(q_pi, self.policies, self.num_controls, self.action_selection, self.alpha, rng_key=rng_key)

        return action, prob_pi