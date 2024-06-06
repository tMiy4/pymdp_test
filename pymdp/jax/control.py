#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=no-member
# pylint: disable=not-an-iterable

import itertools
import jax.numpy as jnp
import jax.tree_util as jtu
from typing import List, Tuple, Optional
from functools import partial
from jax.scipy.special import xlogy
from jax import lax, jit, vmap, nn
from jax import random as jr
from itertools import chain
from jaxtyping import Array

from pymdp.jax.maths import *
# import pymdp.jax.utils as utils

def get_marginals(q_pi, policies, num_controls):
    """
    Computes the marginal posterior(s) over actions by integrating their posterior probability under the policies that they appear within.

    Parameters
    ----------
    q_pi: 1D ``numpy.ndarray``
        Posterior beliefs over policies, i.e. a vector containing one posterior probability per policy.
    policies: ``list`` of 2D ``numpy.ndarray``
        ``list`` that stores each policy as a 2D array in ``policies[p_idx]``. Shape of ``policies[p_idx]`` 
        is ``(num_timesteps, num_factors)`` where ``num_timesteps`` is the temporal
        depth of the policy and ``num_factors`` is the number of control factors.
    num_controls: ``list`` of ``int``
        ``list`` of the dimensionalities of each control state factor.
    
    Returns
    ----------
    action_marginals: ``list`` of ``jax.numpy.ndarrays``
       List of arrays corresponding to marginal probability of each action possible action
    """
    num_factors = len(num_controls)    

    action_marginals = []
    for factor_i in range(num_factors):
        actions = jnp.arange(num_controls[factor_i])[:, None]
        action_marginals.append(jnp.where(actions==policies[:, 0, factor_i], q_pi, 0).sum(-1))
    
    return action_marginals

def sample_action(q_pi, policies, num_controls, action_selection="deterministic", alpha=16.0, rng_key=None):
    """
    Samples an action from posterior marginals, one action per control factor.

    Parameters
    ----------
    q_pi: 1D ``numpy.ndarray``
        Posterior beliefs over policies, i.e. a vector containing one posterior probability per policy.
    policies: ``list`` of 2D ``numpy.ndarray``
        ``list`` that stores each policy as a 2D array in ``policies[p_idx]``. Shape of ``policies[p_idx]`` 
        is ``(num_timesteps, num_factors)`` where ``num_timesteps`` is the temporal
        depth of the policy and ``num_factors`` is the number of control factors.
    num_controls: ``list`` of ``int``
        ``list`` of the dimensionalities of each control state factor.
    action_selection: string, default "deterministic"
        String indicating whether whether the selected action is chosen as the maximum of the posterior over actions,
        or whether it's sampled from the posterior marginal over actions
    alpha: float, default 16.0
        Action selection precision -- the inverse temperature of the softmax that is used to scale the 
        action marginals before sampling. This is only used if ``action_selection`` argument is "stochastic"

    Returns
    ----------
    selected_policy: 1D ``numpy.ndarray``
        Vector containing the indices of the actions for each control factor
    """

    marginal = get_marginals(q_pi, policies, num_controls)
    
    if action_selection == 'deterministic':
        selected_policy = jtu.tree_map(lambda x: jnp.argmax(x, -1), marginal)
    elif action_selection == 'stochastic':
        logits = lambda x: alpha * log_stable(x)
        selected_policy = jtu.tree_map(lambda x: jr.categorical(rng_key, logits(x)), marginal)
    else:
        raise NotImplementedError

    return jnp.array(selected_policy)

def sample_policy(q_pi, policies, num_controls, action_selection="deterministic", alpha = 16.0, rng_key=None):

    if action_selection == "deterministic":
        policy_idx = jnp.argmax(q_pi)
    elif action_selection == "stochastic":
        log_p_policies = log_stable(q_pi) * alpha
        policy_idx = jr.categorical(rng_key, log_p_policies)

    selected_multiaction = policies[policy_idx, 0]
    return selected_multiaction

def construct_policies(num_states, num_controls = None, policy_len=1, control_fac_idx=None):
    """
    Generate a ``list`` of policies. The returned array ``policies`` is a ``list`` that stores one policy per entry.
    A particular policy (``policies[i]``) has shape ``(num_timesteps, num_factors)`` 
    where ``num_timesteps`` is the temporal depth of the policy and ``num_factors`` is the number of control factors.

    Parameters
    ----------
    num_states: ``list`` of ``int``
        ``list`` of the dimensionalities of each hidden state factor
    num_controls: ``list`` of ``int``, default ``None``
        ``list`` of the dimensionalities of each control state factor. If ``None``, then is automatically computed as the dimensionality of each hidden state factor that is controllable
    policy_len: ``int``, default 1
        temporal depth ("planning horizon") of policies
    control_fac_idx: ``list`` of ``int``
        ``list`` of indices of the hidden state factors that are controllable (i.e. those state factors ``i`` where ``num_controls[i] > 1``)

    Returns
    ----------
    policies: ``list`` of 2D ``numpy.ndarray``
        ``list`` that stores each policy as a 2D array in ``policies[p_idx]``. Shape of ``policies[p_idx]`` 
        is ``(num_timesteps, num_factors)`` where ``num_timesteps`` is the temporal
        depth of the policy and ``num_factors`` is the number of control factors.
    """

    num_factors = len(num_states)
    if control_fac_idx is None:
        if num_controls is not None:
            control_fac_idx = [f for f, n_c in enumerate(num_controls) if n_c > 1]
        else:
            control_fac_idx = list(range(num_factors))

    if num_controls is None:
        num_controls = [num_states[c_idx] if c_idx in control_fac_idx else 1 for c_idx in range(num_factors)]
        
    x = num_controls * policy_len
    policies = list(itertools.product(*[list(range(i)) for i in x]))
    
    for pol_i in range(len(policies)):
        policies[pol_i] = jnp.array(policies[pol_i]).reshape(policy_len, num_factors)

    return jnp.stack(policies)


def update_posterior_policies(policy_matrix, qs_init, A, B, C, E, pA, pB, A_dependencies, B_dependencies, gamma=16.0, use_utility=True, use_states_info_gain=True, use_param_info_gain=False):
    # policy --> n_levels_factor_f x 1
    # factor --> n_levels_factor_f x n_policies
    ## vmap across policies
    compute_G_fixed_states = partial(compute_G_policy, qs_init, A, B, C, pA, pB, A_dependencies, B_dependencies,
                                     use_utility=use_utility, use_states_info_gain=use_states_info_gain, use_param_info_gain=use_param_info_gain)

    # only in the case of policy-dependent qs_inits
    # in_axes_list = (1,) * n_factors
    # all_efe_of_policies = vmap(compute_G_policy, in_axes=(in_axes_list, 0))(qs_init_pi, policy_matrix)

    # policies needs to be an NDarray of shape (n_policies, n_timepoints, n_control_factors)
    neg_efe_all_policies = vmap(compute_G_fixed_states)(policy_matrix)

    return nn.softmax(gamma * neg_efe_all_policies + log_stable(E)), neg_efe_all_policies

def compute_expected_state(qs_prior, B, u_t, B_dependencies=None): 
    """
    Compute posterior over next state, given belief about previous state, transition model and action...
    """
    #Note: this algorithm is only correct if each factor depends only on itself. For any interactions, 
    # we will have empirical priors with codependent factors. 
    assert len(u_t) == len(B)  
    qs_next = []
    for B_f, u_f, deps in zip(B, u_t, B_dependencies):
        relevant_factors = [qs_prior[idx] for idx in deps]
        qs_next_f = factor_dot(B_f[...,u_f], relevant_factors, keep_dims=(0,))
        qs_next.append(qs_next_f)
        
    # P(s'|s, u) = \sum_{s, u} P(s'|s) P(s|u) P(u|pi)P(pi) because u </-> pi
    return qs_next

def compute_expected_state_and_Bs(qs_prior, B, u_t): 
    """
    Compute posterior over next state, given belief about previous state, transition model and action...
    """
    assert len(u_t) == len(B)  
    qs_next = []
    Bs = []
    for qs_f, B_f, u_f in zip(qs_prior, B, u_t):
        qs_next.append( B_f[..., u_f].dot(qs_f) )
        Bs.append(B_f[..., u_f])
    
    return qs_next, Bs

def compute_expected_obs(qs, A, A_dependencies):
    """
    New version of expected observation (computation of Q(o|pi)) that takes into account sparse dependencies between observation
    modalities and hidden state factors
    """
        
    def compute_expected_obs_modality(A_m, m):
        deps = A_dependencies[m]
        relevant_factors = [qs[idx] for idx in deps]
        return factor_dot(A_m, relevant_factors, keep_dims=(0,))

    return jtu.tree_map(compute_expected_obs_modality, A, list(range(len(A))))

def compute_info_gain(qs, qo, A, A_dependencies):
    """
    New version of expected information gain that takes into account sparse dependencies between observation modalities and hidden state factors.
    """

    def compute_info_gain_for_modality(qo_m, A_m, m):
        H_qo = - xlogy(qo_m, qo_m).sum()
        # H_qo = - (qo_m * log_stable(qo_m)).sum()
        H_A_m = - xlogy(A_m, A_m).sum(0)
        # H_A_m = - (A_m * log_stable(A_m)).sum(0)
        deps = A_dependencies[m]
        relevant_factors = [qs[idx] for idx in deps]
        qs_H_A_m = factor_dot(H_A_m, relevant_factors)
        return H_qo - qs_H_A_m
    
    info_gains_per_modality = jtu.tree_map(compute_info_gain_for_modality, qo, A, list(range(len(A))))
        
    return jtu.tree_reduce(lambda x,y: x+y, info_gains_per_modality)

# qs_H_A = 0 # expected entropy of the likelihood, under Q(s)
# H_qo = 0 # marginal entropy of Q(o)
# for a, o, deps in zip(A, qo, A_dependencies):
#     relevant_factors = jtu.tree_map(lambda idx: qs[idx], deps)
#     qs_joint_relevant = relevant_factors[0]
#     for q in relevant_factors[1:]:
#         qs_joint_relevant = jnp.expand_dims(qs_joint_relevant, -1) * q
#     H_A_m = -(a * log_stable(a)).sum(0)
#     qs_H_A += (H_A_m * qs_joint_relevant).sum()

#     H_qo -= (o * log_stable(o)).sum()

def compute_expected_utility(qo, C):
    
    util = 0.
    for o_m, C_m in zip(qo, C):
        util += (o_m * C_m).sum()
    
    return util

def calc_pA_info_gain(pA, qo, qs, A_dependencies):
    """
    Compute expected Dirichlet information gain about parameters ``pA`` for a given posterior predictive distribution over observations ``qo`` and states ``qs``.

    Parameters
    ----------
    pA: ``numpy.ndarray`` of dtype object
        Dirichlet parameters over observation model (same shape as ``A``)
    qo: ``list`` of ``numpy.ndarray`` of dtype object
        Predictive posterior beliefs over observations; stores the beliefs about
        observations expected under the policy at some arbitrary time ``t``
    qs: ``list`` of ``numpy.ndarray`` of dtype object
        Predictive posterior beliefs over hidden states, stores the beliefs about
        hidden states expected under the policy at some arbitrary time ``t``

    Returns
    -------
    infogain_pA: float
        Surprise (about Dirichlet parameters) expected for the pair of posterior predictive distributions ``qo`` and ``qs``
    """

    wA = lambda pa: spm_wnorm(pa) * (pa > 0.)
    fd = lambda x, i: factor_dot(x, [s for f, s in enumerate(qs) if f in A_dependencies[i]], keep_dims=(0,))[..., None]
    pA_infogain_per_modality = jtu.tree_map(
        lambda pa, qo, m: qo.dot(fd( wA(pa), m)), pA, qo, list(range(len(qo)))
    )
    infogain_pA = jtu.tree_reduce(lambda x, y: x + y, pA_infogain_per_modality)[0]
    return infogain_pA

def calc_pB_info_gain(pB, qs_t, qs_t_minus_1, B_dependencies, u_t_minus_1):
    """
    Compute expected Dirichlet information gain about parameters ``pB`` under a given policy

    Parameters
    ----------
    pB: ``Array`` of dtype object
        Dirichlet parameters over transition model (same shape as ``B``)
    qs_t: ``list`` of ``Array`` of dtype object
        Predictive posterior beliefs over hidden states expected under the policy at time ``t``
    qs_t_minus_1: ``list`` of ``Array`` of dtype object
        Posterior over hidden states at time ``t-1`` (before receiving observations)
    u_t_minus_1: "Array"
        Actions in time step t-1 for each factor

    Returns
    -------
    infogain_pB: float
        Surprise (about Dirichlet parameters) expected under the policy in question
    """
    
    wB = lambda pb:  spm_wnorm(pb) * (pb > 0.)
    fd = lambda x, i: factor_dot(x, [s for f, s in enumerate(qs_t_minus_1) if f in B_dependencies[i]], keep_dims=(0,))[..., None]
    
    pB_infogain_per_factor = jtu.tree_map(lambda pb, qs, f: qs.dot(fd(wB(pb[..., u_t_minus_1[f]]), f)), pB, qs_t, list(range(len(qs_t))))
    infogain_pB = jtu.tree_reduce(lambda x, y: x + y, pB_infogain_per_factor)[0]
    return infogain_pB

def compute_G_policy(qs_init, A, B, C, pA, pB, A_dependencies, B_dependencies, policy_i, use_utility=True, use_states_info_gain=True, use_param_info_gain=False):
    """ Write a version of compute_G_policy that does the same computations as `compute_G_policy` but using `lax.scan` instead of a for loop. """

    def scan_body(carry, t):

        qs, neg_G = carry

        qs_next = compute_expected_state(qs, B, policy_i[t], B_dependencies)

        qo = compute_expected_obs(qs_next, A, A_dependencies)

        info_gain = compute_info_gain(qs_next, qo, A, A_dependencies) if use_states_info_gain else 0.

        utility = compute_expected_utility(qo, C) if use_utility else 0.

        param_info_gain = calc_pA_info_gain(pA, qo, qs_next) if use_param_info_gain else 0.
        param_info_gain += calc_pB_info_gain(pB, qs_next, qs, policy_i[t]) if use_param_info_gain else 0.

        neg_G += info_gain + utility + param_info_gain

        return (qs_next, neg_G), None

    qs = qs_init
    neg_G = 0.
    final_state, _ = lax.scan(scan_body, (qs, neg_G), jnp.arange(policy_i.shape[0]))
    qs_final, neg_G = final_state
    return neg_G

def compute_G_policy_inductive(qs_init, A, B, C, pA, pB, A_dependencies, B_dependencies, I, policy_i, inductive_epsilon=1e-3, use_utility=True, use_states_info_gain=True, use_param_info_gain=False, use_inductive=False):
    """ 
    Write a version of compute_G_policy that does the same computations as `compute_G_policy` but using `lax.scan` instead of a for loop.
    This one further adds computations used for inductive planning.
    """

    def scan_body(carry, t):

        qs, neg_G = carry

        qs_next = compute_expected_state(qs, B, policy_i[t], B_dependencies)

        qo = compute_expected_obs(qs_next, A, A_dependencies)

        info_gain = compute_info_gain(qs_next, qo, A, A_dependencies) if use_states_info_gain else 0.

        utility = compute_expected_utility(qo, C) if use_utility else 0.

        inductive_value = calc_inductive_value_t(qs_init, qs_next, I, epsilon=inductive_epsilon) if use_inductive else 0.

        param_info_gain = calc_pA_info_gain(pA, qo, qs_next, A_dependencies) if use_param_info_gain else 0.
        param_info_gain += calc_pB_info_gain(pB, qs_next, qs, B_dependencies, policy_i[t]) if use_param_info_gain else 0.

        neg_G += info_gain + utility - param_info_gain + inductive_value

        return (qs_next, neg_G), None

    qs = qs_init
    neg_G = 0.
    final_state, _ = lax.scan(scan_body, (qs, neg_G), jnp.arange(policy_i.shape[0]))
    _, neg_G = final_state
    return neg_G

def update_posterior_policies_inductive(policy_matrix, qs_init, A, B, C, E, pA, pB, A_dependencies, B_dependencies, I, gamma=16.0, inductive_epsilon=1e-3, use_utility=True, use_states_info_gain=True, use_param_info_gain=False, use_inductive=True):
    # policy --> n_levels_factor_f x 1
    # factor --> n_levels_factor_f x n_policies
    ## vmap across policies
    compute_G_fixed_states = partial(compute_G_policy_inductive, qs_init, A, B, C, pA, pB, A_dependencies, B_dependencies, I, inductive_epsilon=inductive_epsilon,
                                     use_utility=use_utility,  use_states_info_gain=use_states_info_gain, use_param_info_gain=use_param_info_gain, use_inductive=use_inductive)

    # only in the case of policy-dependent qs_inits
    # in_axes_list = (1,) * n_factors
    # all_efe_of_policies = vmap(compute_G_policy, in_axes=(in_axes_list, 0))(qs_init_pi, policy_matrix)

    # policies needs to be an NDarray of shape (n_policies, n_timepoints, n_control_factors)
    neg_efe_all_policies = vmap(compute_G_fixed_states)(policy_matrix)

    return nn.softmax(gamma * neg_efe_all_policies + log_stable(E)), neg_efe_all_policies

def generate_I_matrix(H: List[Array], B: List[Array], threshold: float, depth: int):
    """ 
    Generates the `I` matrices used in inductive planning. These matrices stores the probability of reaching the goal state backwards from state j (columns) after i (rows) steps.
    Parameters
    ----------    
    H: ``list`` of ``jax.numpy.ndarray``
        Constraints over desired states (1 if you want to reach that state, 0 otherwise)
    B: ``list`` of ``jax.numpy.ndarray``
        Dynamics likelihood mapping or 'transition model', mapping from hidden states at ``t`` to hidden states at ``t+1``, given some control state ``u``.
        Each element ``B[f]`` of this object array stores a 3-D tensor for hidden state factor ``f``, whose entries ``B[f][s, v, u]`` store the probability
        of hidden state level ``s`` at the current time, given hidden state level ``v`` and action ``u`` at the previous time.
    threshold: ``float``
        The threshold for pruning transitions that are below a certain probability
    depth: ``int``
        The temporal depth of the backward induction

    Returns
    ----------
    I: ``numpy.ndarray`` of dtype object
        For each state factor, contains a 2D ``numpy.ndarray`` whose element i,j yields the probability 
        of reaching the goal state backwards from state j after i steps.
    """
    
    num_factors = len(H)
    I = []
    for f in range(num_factors):
        """
        For each factor, we need to compute the probability of reaching the goal state
        """

        # If there exists an action that allows transitioning 
        # from state to next_state, with probability larger than threshold
        # set b_reachable[current_state, previous_state] to 1
        b_reachable = jnp.where(B[f] > threshold, 1.0, 0.0).sum(axis=-1)
        b_reachable = jnp.where(b_reachable > 0., 1.0, 0.0)

        def step_fn(carry, i):
            I_prev = carry
            I_next = jnp.dot(b_reachable, I_prev)
            I_next = jnp.where(I_next > 0.1, 1.0, 0.0) # clamp I_next to 1.0 if it's above 0.1, 0 otherwise
            return I_next, I_next
    
        _, I_f = lax.scan(step_fn, H[f], jnp.arange(depth-1))
        I_f = jnp.concatenate([H[f][None,...], I_f], axis=0)

        I.append(I_f)
    
    return I

def calc_inductive_value_t(qs, qs_next, I, epsilon=1e-3):
    """
    Computes the inductive value of a state at a particular time (translation of @tverbele's `numpy` implementation of inductive planning, formerly
    called `calc_inductive_cost`).

    Parameters
    ----------
    qs: ``list`` of ``jax.numpy.ndarray`` 
        Marginal posterior beliefs over hidden states at a given timepoint.
    qs_next: ```list`` of ``jax.numpy.ndarray`` 
        Predictive posterior beliefs over hidden states expected under the policy.
    I: ``numpy.ndarray`` of dtype object
        For each state factor, contains a 2D ``numpy.ndarray`` whose element i,j yields the probability 
        of reaching the goal state backwards from state j after i steps.
    epsilon: ``float``
        Value that tunes the strength of the inductive value (how much it contributes to the expected free energy of policies)

    Returns
    -------
    inductive_val: float
        Value (negative inductive cost) of visiting this state using backwards induction under the policy in question
    """
    
    # initialise inductive value
    inductive_val = 0.

    log_eps = log_stable(epsilon)
    for f in range(len(qs)):
        # we also assume precise beliefs here?!
        idx = jnp.argmax(qs[f])
        # m = arg max_n p_n < sup p

        # i.e. find first entry at which I_idx equals 1, and then m is the index before that
        m = jnp.maximum(jnp.argmax(I[f][:, idx])-1, 0)
        I_m = (1. - I[f][m, :]) * log_eps
        path_available = jnp.clip(I[f][:, idx].sum(0), min=0, max=1) # if there are any 1's at all in that column of I, then this == 1, otherwise 0
        inductive_val += path_available * I_m.dot(qs_next[f]) # scaling by path_available will nullify the addition of inductive value in the case we find no path to goal (i.e. when no goal specified)

    return inductive_val

# if __name__ == '__main__':

#     from jax import random as jr
#     key = jr.PRNGKey(1)
#     num_obs = [3, 4]

#     A = [jr.uniform(key, shape = (no, 2, 2)) for no in num_obs]
#     B = [jr.uniform(key, shape = (2, 2, 2)), jr.uniform(key, shape = (2, 2, 2))]
#     C = [log_stable(jnp.array([0.8, 0.1, 0.1])), log_stable(jnp.ones(4)/4)]
#     policy_1 = jnp.array([[0, 1],
#                          [1, 1]])
#     policy_2 = jnp.array([[1, 0],
#                          [0, 0]])
#     policy_matrix = jnp.stack([policy_1, policy_2]) # 2 x 2 x 2 tensor
    
#     qs_init = [jnp.ones(2)/2, jnp.ones(2)/2]
#     neg_G_all_policies = jit(update_posterior_policies)(policy_matrix, qs_init, A, B, C)
#     print(neg_G_all_policies)

def compute_predicted_KLD(qs, qo, A, A_dependencies):
    """
    New version of expected information gain that takes into account sparse dependencies between observation modalities and hidden state factors.
    """

    def compute_pKLD_for_modality(qo_m, A_m, m):
        H_qo = - (qo_m * log_stable(qo_m)).sum()
        #print("行列計算")
        #H_qo_A_m = - (qo_m * log_stable(A_m)).sum(0)
        #H_qo_A_m = - factor_dot(log_stable(A_m), qo_m,keep_dims=(1,))#行列計算
        deps = A_dependencies[m]
        relevant_factors = [qs[idx] for idx in deps]
        qs_ln_A_m = factor_dot(log_stable(A_m), relevant_factors, keep_dims=(0,))
        #print("factor_dot")
        qo_qs_ln_A_m = -(qo_m * qs_ln_A_m).sum()
        #qs_H_A_m = factor_dot(H_qo_A_m, relevant_factors)
        return qo_qs_ln_A_m - H_qo  #Σqolnqo-ΣqsΣqolnA_m
    
    pKLD_per_modality = jtu.tree_map(compute_pKLD_for_modality, qo, A, list(range(len(A))))
        
    return jtu.tree_reduce(lambda x,y: x+y, pKLD_per_modality)

def compute_predicted_free_energy(qs, qo, A, A_dependencies):
    """
    New version of expected information gain that takes into account sparse dependencies between observation modalities and hidden state factors.
    """

    def compute_pF_for_modality(qo_m, A_m, m):
        #H_qo = - (qo_m * log_stable(qo_m)).sum()
        deps = A_dependencies[m]
        relevant_factors = [qs[idx] for idx in deps]
        qs_ln_A_m = factor_dot(log_stable(A_m), relevant_factors, keep_dims=(0,))
        qo_qs_ln_A_m= - (qo_m * qs_ln_A_m).sum()
        return qo_qs_ln_A_m  #-ΣqsΣqolnA_m
    
    pF_per_modality = jtu.tree_map(compute_pF_for_modality, qo, A, list(range(len(A))))
        
    return jtu.tree_reduce(lambda x,y: x+y, pF_per_modality)

def compute_oRisk(qo, C):
    def compute_entropy_expectation_for_modality(qo_m):
        H_qo = - (qo_m * log_stable(qo_m)).sum()
        return H_qo

    oRisk = 0.
    #H_qo = - (qo * log_stable(qo)).sum()
    for o_m, C_m in zip(qo, C):
        oRisk -= (o_m * C_m).sum()
        #util -= (o_m * log_stable(o_m)).sum()
    Entropy_per_modality = jtu.tree_map(compute_entropy_expectation_for_modality, qo)
    H_qo_all=jtu.tree_reduce(lambda x,y: x+y, Entropy_per_modality)#-Σqolnqo
    oRisk-=H_qo_all#Σqolnqo
    return oRisk

def compute_G_policy_inductive_efev_full(qs_init, A, B, C, pA, pB, A_dependencies, B_dependencies, I, policy_i, inductive_epsilon=1e-3, use_utility=True, use_states_info_gain=True, use_param_info_gain=False, use_inductive=False):
    """ 
    Write a version of compute_G_policy that does the same computations as `compute_G_policy` but using `lax.scan` instead of a for loop.
    This one further adds computations used for inductive planning.
    """
    def normalize_vectors(vectors):
            normalized_vectors = []
            for v in vectors:
                sum_v = jnp.sum(v)
                # 0より大きい場合はvをsum_vで割り、そうでない場合はvをそのまま使用
                normalized_v = jnp.where(sum_v > 0, v / sum_v, v)
                normalized_vectors.append(normalized_v)
            return normalized_vectors
    
    

       
    def scan_body(carry, t):

        qs, neg_G, info_gain, predicted_KLD, predicted_F, oRisk, param_info_gainA, param_info_gainB = carry
        
        qs_next = compute_expected_state(qs, B, policy_i[t], B_dependencies)
        
        qo = compute_expected_obs(qs_next, A, A_dependencies)

        #qo = jnp.divide(qo, qo.sum(axis=0))
        # qo内の各ベクトルを正規化
        #qo = normalize_vectors(qo)
        #qs_next = normalize_vectors(qs_next)


        
        info_gain += compute_info_gain(qs_next, qo, A, A_dependencies) if use_states_info_gain else 0.
        #print("PKLD")
        predicted_KLD += compute_predicted_KLD(qs_next, qo, A, A_dependencies) 
        #print("PFE")
        predicted_F += compute_predicted_free_energy(qs_next, qo, A, A_dependencies) 
        #print("Risk")
        oRisk += compute_oRisk(qo, C)
        #utility = compute_expected_utility(qo, C) if use_utility else 0.
        
        
        inductive_value = calc_inductive_value_t(qs_init, qs_next, I, epsilon=inductive_epsilon) if use_inductive else 0.
        
        
        param_info_gainA -= calc_pA_info_gain(pA, qo, qs_next, A_dependencies) if use_param_info_gain else 0.
        
        param_info_gainB -= calc_pB_info_gain(pB, qs_next, qs, B_dependencies, policy_i[t]) if use_param_info_gain else 0.
        #param_info_gainB = calc_pB_info_gain(pB, qs_next, qs) if use_param_info_gain else 0.
        
        
        neg_G = info_gain + predicted_KLD - predicted_F - oRisk + param_info_gainA + param_info_gainB 
        neg_G += inductive_value
        
        return (qs_next, neg_G, info_gain, predicted_KLD, predicted_F, oRisk, param_info_gainA, param_info_gainB), None

    qs = qs_init
    neg_G = 0.
    info_gain = 0.
    predicted_KLD = 0.
    predicted_F = 0.
    oRisk = 0.
    param_info_gainA = 0.
    param_info_gainB = 0.
    
    final_state, _ = lax.scan(scan_body, (qs, neg_G, info_gain, predicted_KLD, predicted_F, oRisk, param_info_gainA, param_info_gainB), jnp.arange(policy_i.shape[0]))
    
    qs_final, neg_G, info_gain, predicted_KLD, predicted_F, oRisk, param_info_gainA, param_info_gainB = final_state
    return neg_G, info_gain, predicted_KLD, predicted_F, oRisk, param_info_gainA, param_info_gainB

def update_posterior_policies_inductive_efev_full(policy_matrix, qs_init, A, B, C, E, pA, pB, A_dependencies, B_dependencies, I, gamma=16.0, inductive_epsilon=1e-3, use_utility=True, use_states_info_gain=True, use_param_info_gain=False, use_inductive=True):
    # policy --> n_levels_factor_f x 1
    # factor --> n_levels_factor_f x n_policies
    ## vmap across policies
    
    compute_G_fixed_states = partial(compute_G_policy_inductive_efev_full, qs_init, A, B, C, pA, pB, A_dependencies, B_dependencies, I, inductive_epsilon=inductive_epsilon,
                                     use_utility=use_utility,  use_states_info_gain=use_states_info_gain, use_param_info_gain=use_param_info_gain, use_inductive=use_inductive)
    
    
    # policies needs to be an NDarray of shape (n_policies, n_timepoints, n_control_factors)
    results = vmap(compute_G_fixed_states)(policy_matrix)
    
    
    neg_efe_all_policies = results[0]  # 各ポリシーの負の期待自由エネルギー
    PBS_a_p = results[1]  # 状態情報利得
    PKLD_a_p = results[2]  # 状態情報利得
    PFE_a_p = results[3]  # 状態情報利得
    oRisk_a_p = results[4]  # 状態情報利得
    PBS_pA_a_p = results[5]  # パラメータAに関する情報利得
    PBS_pB_a_p = results[6]  # パラメータBに関する情報利得
    
        
    

    return nn.softmax(gamma * neg_efe_all_policies + log_stable(E)), neg_efe_all_policies, PBS_a_p, PKLD_a_p, PFE_a_p, oRisk_a_p, PBS_pA_a_p, PBS_pB_a_p

def sample_policy_prob_pi(q_pi, policies, num_controls, action_selection="deterministic", alpha = 16.0, rng_key=None):

    if action_selection == "deterministic":
        policy_idx = jnp.argmax(q_pi)
        p_policies = None
    elif action_selection == "stochastic":
        #p_policies = nn.softmax(log_stable(q_pi) * alpha)
        p_policies = log_stable(q_pi) * alpha
        policy_idx = jr.categorical(rng_key, p_policies)
        p_policies = nn.softmax(log_stable(q_pi) * alpha)

    selected_multiaction = policies[policy_idx, 0]
    return selected_multiaction, p_policies