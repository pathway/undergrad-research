from __future__ import print_function
from collections import namedtuple
import random
import numpy as np
import tensorflow as tf
from model import LSTMPolicy, FuNPolicy
import six.moves.queue as queue
import scipy.signal
import scipy.spatial
import threading
import distutils.version
import logging
use_tf12_api = distutils.version.LooseVersion(tf.VERSION) >= distutils.version.LooseVersion('0.12.0')

import sys

LEARNING_RATE = 1e-4
ALPHA = .99
LOCAL_STEPS = 40
HORIZEN_C = 10
INTRINSIC_INFLUENCE = .8 # NOTE: from the paper, somewhere between 0 and 1....


EPSILON_orig = .3
EPSILON = .3
EPSILON_FINAL = .05
ANNEAL_STEPS = 10000000 
#EPSILON_STEP = (EPSILON / 2) / ANNEAL_STEPS
EPSILON_STEP = (EPSILON_orig - EPSILON_FINAL) / ANNEAL_STEPS


WORKER_DISCOUNT = .95
MANAGER_DISCOUNT = .99


def discount(x, gamma):
    return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

# https://gist.github.com/ranarag/77014b952a649dbaf8f47969affdd3bc
#def cosine_sim(x1, x2,name = 'Cosine_loss'): # axis is 1 by default
#def cosine_sim(x1, x2,  axis, name='Cosine_loss'):
    #with tf.name_scope(name):
        #if axis == 2:
            ## TODO: TODO: TODO: TODO: TODO: TODO: TODO: is setting axis to 1 even valid? Pretty sure it should still be 2?
            #x1_val = tf.sqrt(tf.reduce_sum(tf.matmul(x1,tf.transpose(x1, [0, 2, 1])),axis=2)) # NOTE: axis used to be axix (2, rather than 1)
            #x2_val = tf.sqrt(tf.reduce_sum(tf.matmul(x2,tf.transpose(x2, [0, 2, 1])),axis=2)) 
        #else:
            #x1_val = tf.sqrt(tf.reduce_sum(tf.matmul(x1,tf.transpose(x1)),axis=axis))
            #x2_val = tf.sqrt(tf.reduce_sum(tf.matmul(x2,tf.transpose(x2)),axis=axis))
        ## NOTE: remove pesky nan's....?
        #x1_val = tf.where(tf.is_nan(x1_val), tf.zeros_like(x1_val), x1_val)
        #x2_val = tf.where(tf.is_nan(x2_val), tf.zeros_like(x2_val), x2_val)
        #denom =  tf.multiply(x1_val,x2_val) + .00001 # NOTE: adding to avoid division by 0
        ##print(denom.shape)
        #num = tf.reduce_sum(tf.multiply(x1,x2),axis=axis)
        ##print(num.shape)
        ##return tf.Print(tf.div(num,denom), [num, denom, x1_val, x2_val], message="cosine_sim returns:", summarize=256)
        #return tf.div(num,denom)
#
    # TODO: verify this (probably axis is actually 2, not 1)

# https://gist.github.com/ranarag/77014b952a649dbaf8f47969affdd3bc
def cosine_sim(x1, x2, axis, name="Cosine_loss"):
    with tf.name_scope(name):
        x1_val = tf.sqrt(tf.reduce_sum(tf.matmul(x1,tf.transpose(x1)),axis=1))
        x2_val = tf.sqrt(tf.reduce_sum(tf.matmul(x2,tf.transpose(x2)),axis=1))
        denom =  tf.multiply(x1_val,x2_val)
        num = tf.reduce_sum(tf.multiply(x1,x2),axis=1)
        result = tf.div(num, denom)
        result = tf.where(tf.is_nan(result), tf.zeros_like(result), result)
        return result
    

# https://stackoverflow.com/questions/38061080/how-to-transform-vector-into-unit-vector-in-tensorflow
def cosine_sim_deep(x1, x2):
    with tf.name_scope('Cosine_loss_deep'):
        x1_val = tf.norm(x1, axis=2)
        x2_val = tf.norm(x2, axis=2)

        denom = x1_val * x2_val
        num = tf.reduce_sum(tf.multiply(x1,x2),axis=2)
        #num = tf.reduce_sum(tf.multiply(x1,tf.transpose(x2, [0,2,1])),axis=2)
        #num = tf.tensordot(x1, x2, axes=2)

        
        #return tf.div(num, denom)
        #return tf.Print(tf.div(num,denom), [x1_val, x2_val], message="cosine_sim returns:", summarize=256)
        result = tf.div(num, denom)
        result = tf.where(tf.is_nan(result), tf.zeros_like(result), result) # TODO: is this just hiding the mess or a valid way of dealing with zero vectors??
        #return tf.Print(result, [result, num, denom], message="cosine_sim returns:", summarize=256)
        return result
    
        #return tf.Print(tf.div(num,denom), [x1, x2], message="cosine_sim returns:", summarize=256)
        
    

def process_rollout(rollout, sess, gamma, lambda_=1.0):
    """
given a rollout, compute its returns and the advantage
"""
    # need: intrinsic reward, both batch advantages

    batch_states = np.asarray(rollout.states)
    batch_actions = np.asarray(rollout.actions)
    
    latent_states = np.asarray(rollout.m_states)
    latent_states = np.squeeze(latent_states)
    goals = np.asarray(rollout.goals)
    
    # NOTE: this causes weird issue if you don't specify axis (if for some reason, only a single set of goals is coming in, then that initial 1 dimension would also be removed, which breaks the vstack below in goal_hist_stack)
    goals = np.squeeze(goals) # remove that random 1 dimension in the middle 
    if goals.shape[0] == 0 or len(goals.shape) < 2: # TODO: This is sometimes randomly the case? Still not sure why
        print("=========== ERROR - BAD SHAPE")
        return -1
    
        
    
    rewards = np.asarray(rollout.rewards) # NOTE: environment rewards
    pred_v_m = np.asarray(rollout.values_m + [rollout.r])
    #pred_v_m = np.squeeze(pred_v_m) 

    pred_v_w = np.asarray(rollout.values_w + [rollout.r]) # NOTE: making assumption that this is correct? Do we need to handle an additional bootstrapped intrinsic reward?
    #print(pred_v_w.shape)
    pred_v_w = np.squeeze(pred_v_w) 
    #print(pred_v_w)

    rewards_plus_v = np.asarray(rollout.rewards + [rollout.r])
    #batch_reward = discount(rewards_plus_v, gamma)[:-1] # NOTE: target_v, right?
    batch_reward = discount(rewards_plus_v, gamma)[:-1] # NOTE: target_v, right?

    delta_t_m = rewards + MANAGER_DISCOUNT*pred_v_m[1:] - pred_v_m[:-1]
    batch_adv_m = discount(delta_t_m, MANAGER_DISCOUNT)


    # TODO: don't know if this is right, but it has one too many values
    #pred_v_w = pred_v_w[:-1]
    pred_v_w = pred_v_w[1:]

    

    # TODO: fancy stacking of goals for g_hist # NOTE: this is still probably super slow
    goal_hist_stack = [goals]
    #print("Goals:")
    #print(goals)
    #print("goals size:", goals.shape)
    size = len(goals)
    #print("Size:", size)
    for i in range(HORIZEN_C):
        #print("Hist size:", len(goal_hist_stack[-1][:-1]))
        #goal_hist_stack.append(np.vstack((np.zeros((1,3)), goal_hist_stack[-1][:-1])))

        # TODO: below line is breaking? "all input array dimensions except for concatenation axis must be exactly equal 
        #print(goal_hist_stack[-1])
        #print(goal_hist_stack[-1][:-1].shape)
        #goal_hist_stack.append(np.vstack((np.zeros((1,256)), goal_hist_stack[-1][:-1]))) # TODO: old
        goal_hist_stack.append(np.vstack((rollout.old_goals[HORIZEN_C - 1 - i], goal_hist_stack[-1][:-1])))
    goal_hist = np.flip(np.rot90(np.dstack(goal_hist_stack), 3, (1,2)), axis=2)[:,1:]

    # TODO: calculate horizen state differnecs # NOTE: this is still probably super slow
    lstate_hist_stack = [latent_states]
    #print("Latent state size:",latent_states.shape)
    for i in range(HORIZEN_C):
        #lstate_hist_stack.append(np.vstack((np.zeros((1,256)), lstate_hist_stack[-1][:-1])))
        lstate_hist_stack.append(np.vstack((rollout.old_latent_states[HORIZEN_C - 1 - i], lstate_hist_stack[-1][:-1])))
    lstate_hist = np.flip(np.rot90(np.dstack(lstate_hist_stack), 3, (1,2)), axis=2)[:,1:]
    latent_states_sub = np.rot90(np.repeat(latent_states[:,:,np.newaxis], HORIZEN_C, axis=2), 1, (1,2)) 
    state_diffs = latent_states_sub - lstate_hist

    # TODO: calculate g_dist, single cosine similarity between goals and latent states (but for each entry, so end with an array, right?
    # NOTE: no, actually just do this in tensorflow since we already have a thing for it


    # NOTE: pretty sure the cosine_sim might still be along wrong dimension down below?


    #print("G_hist:",goal_hist)
    #print("s_diff:", state_diffs)
    


    features_m = rollout.features_m[0]
    features_w = rollout.features_w[0]

    
    #print("reward shape:", batch_reward.shape)
    #print("v_w shape:", pred_v_w.shape)
    #print("goals shape:", goals.shape)
    #print("hist shape:", goal_hist.shape)
    #print("state diffs shape:", state_diffs.shape)
    #print("adv_m shape:", batch_adv_m.shape)


    # get intrinsic reward and calculate worker advantage
    #sess.run(self.r_intrinsic, feed_dict={
        #gt: goals

    

    return Batch(batch_states, batch_actions, batch_reward, batch_adv_m, pred_v_w, goals, goal_hist, state_diffs, latent_states, rollout.terminal, features_w, features_m)

    # calculate intrinsic reward TODO: this can't efficiently be done here, throw calculations into tensorflow loss graph
    #reward_intrinsic = 0
    #for i in range(HORIZEN_C):
        #reward_intrinsic += scipy.spatial.distance.cosine(latent_states[i], goals[i])
    #reward_intrinsic /= HORIZEN_C

    #delta_t_w = rewards

    

    '''
    batch_si = np.asarray(rollout.states)
    batch_a = np.asarray(rollout.actions)
    rewards = np.asarray(rollout.rewards)
    vpred_t = np.asarray(rollout.values + [rollout.r])

    rewards_plus_v = np.asarray(rollout.rewards + [rollout.r])
    batch_r = discount(rewards_plus_v, gamma)[:-1] # NOTE: target_v, right?
    delta_t = rewards + gamma * vpred_t[1:] - vpred_t[:-1]
    # this formula for the advantage comes "Generalized Advantage Estimation":
    # https://arxiv.org/abs/1506.02438
    batch_adv = discount(delta_t, gamma * lambda_)

    features = rollout.features[0]
    return Batch(batch_si, batch_a, batch_adv, batch_r, rollout.terminal, features)
    '''

#Batch = namedtuple("Batch", ["si", "a", "adv", "r", "terminal", "features"])
Batch = namedtuple("Batch", ["si", "ac", "r", "adv_m", "v_w", "gt", "g_hist", "s_diff", "latent_states", "terminal", "features_w", "features_m"])

class PartialRollout(object):
    """
a piece of a complete rollout.  We run our agent, and process its experience
once it has processed enough steps.
"""
    def __init__(self):
        self.states = []
        self.m_states = [] # NOTE: this is s_t for the manager (latent states)
        self.actions = []
        self.rewards = []
        self.values_w = []
        self.values_m = []
        self.r = 0.0
        self.terminal = False
        self.features_w = [] 
        self.features_m = [] 
        self.goals = []
        self.old_latent_states = []
        self.old_goals = []

    def add(self, state, m_state, action, reward, value_w, value_m, terminal, features_w, features_m, goals):
        self.states += [state]
        self.m_states += [m_state]
        self.actions += [action]
        self.rewards += [reward]
        self.values_w += [value_w]
        self.values_m += [value_m]
        self.terminal = terminal
        self.features_w += [features_w]
        self.features_m += [features_m]
        self.goals += [goals]

    def extend(self, other):
        assert not self.terminal
        self.states.extend(other.states)
        self.m_states.extend(other.m_states)
        self.actions.extend(other.actions)
        self.rewards.extend(other.rewards)
        self.values_w.extend(other.values_w)
        self.values_m.extend(other.values_m)
        self.r = other.r # TODO: was this already like this? Or did I do this?
        self.terminal = other.terminal
        self.features_w.extend(other.features_w)
        self.features_m.extend(other.features_m)
        self.goals.extend(other.goals)

class RunnerThread(threading.Thread):
    """
One of the key distinctions between a normal environment and a universe environment
is that a universe environment is _real time_.  This means that there should be a thread
that would constantly interact with the environment and tell it what to do.  This thread is here.
"""
    def __init__(self, env, policy, num_local_steps, visualise, renderOnly=False):
        threading.Thread.__init__(self)
        self.queue = queue.Queue(5)
        self.num_local_steps = num_local_steps
        self.env = env
        self.last_features = None
        self.policy = policy
        self.daemon = True
        self.sess = None
        self.summary_writer = None
        self.visualise = visualise
        self.renderOnly = renderOnly

    def start_runner(self, sess, summary_writer):
        self.sess = sess
        self.summary_writer = summary_writer
        self.start()

    def run(self):
        with self.sess.as_default():
            self._run()

    def _run(self):
        rollout_provider = env_runner(self.env, self.policy, self.num_local_steps, self.summary_writer, self.visualise, self.renderOnly)
        while True:
            # the timeout variable exists because apparently, if one worker dies, the other workers
            # won't die with it, unless the timeout is set to some large number.  This is an empirical
            # observation.

            self.queue.put(next(rollout_provider), timeout=600.0)


def env_runner(env, policy, num_local_steps, summary_writer, render, renderOnly):
    global EPSILON
    global EPSILON_orig
    global EPSILON_STEP
    global EPSILON_FINAL
    """
The logic of the thread runner.  In brief, it constantly keeps on running
the policy, and as long as the rollout exceeds a certain length, the thread
runner appends the policy to the queue.
"""
    last_state = env.reset()
    last_features = policy.get_initial_features()
    #last_features_w_c, last_features_w_h = policy.get_initial_features()
    length = 0
    rewards = 0

    renderOnly = False
    if renderOnly:
        sys.stdout = open('out.txt', 'a')
        print("BEGINNING LOG OF RENDER ONLY")
        sys.stdout.flush()

        sys.stderr = open('error.txt', 'a')
        sys.stderr.flush()

        #print(last_state.shape())
        last_state = last_state[0]
        #print(last_state.shape())
        #sys.stdout.flush()
        
        while True:
            terminal_end = False
            rollout = PartialRollout()

            print("Inside loop")
            sys.stdout.flush()

            for _ in range(num_local_steps):
                print("inside for")
                sys.stdout.flush()
                fetched = policy.act(last_state, *last_features)
                action, value_w, value_m, goals, features_w, features_m, latent_state = fetched[0], fetched[1], fetched[2], fetched[3], fetched[4], fetched[5], fetched[6],
                # argmax to convert from one-hot
                print(action)
                sys.stdout.flush()
                print(action.argmax())
                
                state, reward, terminal, info = env.step(action.argmax())
                
                print("Rendering!")
                sys.stdout.flush()
                env.render()

                # NOTE: reward clipping
                actual_score = reward
                if (reward > 1.0): reward = 1.0
                if (reward < -1.0): reward = -1.0
                
                # collect the experience
                rollout.add(last_state, latent_state, action, reward, value_w, value_m, terminal, features_w, features_m, goals)
                length += 1
                rewards += actual_score

                last_state = state
                last_features = [features_w[0], features_w[1], features_m[0], features_m[1]]

                if info:
                    summary = tf.Summary()
                    for k, v in info.items():
                        summary.value.add(tag=k, simple_value=float(v))
                    summary_writer.add_summary(summary, policy.global_step.eval())
                    summary_writer.flush()

                timestep_limit = env.spec.tags.get('wrapper_config.TimeLimit.max_episode_steps')
                if terminal or length >= timestep_limit:
                    terminal_end = True
                    if length >= timestep_limit or not env.metadata.get('semantics.autoreset'):
                        last_state = env.reset()
                    last_features = policy.get_initial_features()
                    print("Episode finished. Sum of rewards: %d. Length: %d" % (rewards, length))
                    length = 0
                    rewards = 0
                    break

            if not terminal_end:
                rollout.r = policy.value(last_state, *last_features)

            # once we have enough experience, yield it, and have the ThreadRunner place it on a queue
            yield rollout

    else: 
        counter = 0
        while True:
            terminal_end = False
            rollout = PartialRollout()

            old_latent_states = np.zeros((HORIZEN_C, 256)) # history to pass in
            old_goals = np.zeros((HORIZEN_C, 256)) # history to pass in


            for _ in range(num_local_steps):
                #print("going in w:", last_features[0])
                fetched = policy.act(last_state, *last_features)
                action, value_w, value_m, goals, features_w, features_m, latent_state = fetched[0], fetched[1], fetched[2], fetched[3], fetched[4], fetched[5], fetched[6]

                # epsilon prob of random action
                randomOrNot = random.uniform(0, 1)
                if randomOrNot < EPSILON:
                    #print("RANDOM ACTION")
                    action = np.array([0.0] * env.action_space.n)
                    action[env.action_space.sample()] = 1.0
                    if EPSILON > EPSILON_FINAL: EPSILON -= EPSILON_STEP
                
                # argmax to convert from one-hot
                state, reward, terminal, info = env.step(action.argmax())
                if render:
                    env.render()

                # NOTE: reward clipping
                actual_score = reward
                if (reward > 1.0): reward = 1.0
                if (reward < -1.0): reward = -1.0

                #print("last fetched w:", features_w[0])
                if True in np.isnan(features_w):
                    print("======== FOUND NAN =========")
                    print("Counter:", counter)
                    print(fetched)
                    return

                # collect the experience
                rollout.add(last_state, latent_state, action, reward, value_w, value_m, terminal, features_w, features_m, goals)
                length += 1
                rewards += actual_score

                last_state = state
                last_features = [features_w[0], features_w[1], features_m[0], features_m[1]]

                if info:
                    summary = tf.Summary()
                    for k, v in info.items():
                        summary.value.add(tag=k, simple_value=float(v))
                    summary_writer.add_summary(summary, policy.global_step.eval())
                    summary_writer.flush()

                timestep_limit = env.spec.tags.get('wrapper_config.TimeLimit.max_episode_steps')
                if terminal or length >= timestep_limit:
                    terminal_end = True
                    if length >= timestep_limit or not env.metadata.get('semantics.autoreset'):
                        last_state = env.reset()
                    last_features = policy.get_initial_features()
                    print("Episode finished. Sum of rewards: %d. Length: %d" % (rewards, length))
                    length = 0
                    rewards = 0
                    old_latent_states = np.zeros((HORIZEN_C, 256)) # history to pass in
                    old_goals = np.zeros((HORIZEN_C, 256)) # history to pass in
                    break

            if not terminal_end:
                rollout.r = policy.value(last_state, *last_features)

            # add in the history
            rollout.old_latent_states = old_latent_states
            rollout.old_goals = old_goals

            # once we have enough experience, yield it, and have the ThreadRunner place it on a queue
            counter += 1
            yield rollout

            old_latent_states = rollout.m_states[-10:]

class A3C(object):
    def __init__(self, env, task, visualise, renderOnly=False):
        """
An implementation of the A3C algorithm that is reasonably well-tuned for the VNC environments.
Below, we will have a modest amount of complexity due to the way TensorFlow handles data parallelism.
But overall, we'll define the model, specify its inputs, and describe how the policy gradients step
should be computed.
"""

        print("---- A3C OBJECT BEING INITIALIZED ----")
        sys.stdout.flush()

        self.env = env
        self.task = task
        worker_device = "/job:worker/task:{}/cpu:0".format(task)
        with tf.device(tf.train.replica_device_setter(1, worker_device=worker_device)):
            with tf.variable_scope("global"):
                self.network = FuNPolicy(env.observation_space.shape, env.action_space.n, LOCAL_STEPS, HORIZEN_C)
                self.global_step = tf.get_variable("global_step", [], tf.int32, initializer=tf.constant_initializer(0, dtype=tf.int32), trainable=False)
                #self.init_uninit = tf.tf.report_uninitialized_variables(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable().name))

        with tf.device(worker_device):
            with tf.variable_scope("local"):
                self.local_network = pi = FuNPolicy(env.observation_space.shape, env.action_space.n, LOCAL_STEPS, HORIZEN_C)
                pi.global_step = self.global_step

            self.r = tf.placeholder(tf.float32, [None], name='r') # NOTE: same for both manager and worker, but worker has the additional intrinsic reward # TODO: which has to be calculated here!!!
            
            self.ac = tf.placeholder(tf.float32, [None, env.action_space.n], name="ac")
            #self.adv_w = tf.placeholder(tf.float32, [None], name='adv_w') # NOTE: NO, not getting passed in. has to be calcluated here
            self.v_w = tf.placeholder(tf.float32, [None], name='v_w')


            # NOTE: yes, getting manager advantage passed in
            self.adv_m = tf.placeholder(tf.float32, [None], name='adv_m')





            # CALCULATING INTRINSIC REWARD
            # ------------------------------------------------

            # TODO: wait, shouldn't these be HORIZEN_C, 256?
            self.g_hist = tf.placeholder(tf.float32, [None, HORIZEN_C, 256], name='g_hist') # TODO: calc in process_rollout
            self.s_diff = tf.placeholder(tf.float32, [None, HORIZEN_C, 256], name='s_diff') # TODO:: calc in process_rollout
            
            #cos_similarity = cosine_sim(self.s_diff, self.g_hist, 2)
            cos_similarity = cosine_sim_deep(self.s_diff, self.g_hist)
            
            self.r_intrinsic = tf.reduce_sum(cos_similarity) / HORIZEN_C

            # ------------------------------------------------



            self.gt = tf.placeholder(tf.float32, [None, 256], name='gt') 

            #self.g_dist = tf.placeholder(tf.float32, [None], name='g_dist') # TODO: calc in process_rollout (should just be a single cosine similarity calculation)
            self.latent_states = tf.placeholder(tf.float32, [None, 256], name='latent_states')


            #self.epsilon = tf.placeholder(tf.float32, [None], name='epsilon')

            self.g_dist = cosine_sim(self.latent_states, self.gt, 1)

            # intrinsic reward for worker NOTE: unsure if this cos similarity is correct, got it from SO

            # https://stackoverflow.com/questions/43357732/how-to-calculate-the-cosine-similarity-between-two-tensors
            #normalize_a = tf.nn.l2_normalize(s_diff,0)        
            #normalize_b = tf.nn.l2_normalize(g_hist,0)
            #cos_similarity = tf.reduce_sum(tf.multiply(normalize_a,normalize_b))
            


            # advantage for worker
            #self.adv_w = self.r + INTRINSIC_INFLUENCE*self.r_intrinsic + self.v_w # [None] (scalar result)
            self.adv_w = tf.placeholder(tf.float32, [None], name='adv_w')


            # TODO: gradients for g_t
            gt_loss = tf.reduce_sum(self.g_dist*self.adv_m)

            v_loss_m = .5 * tf.reduce_sum(tf.square(pi.m_vf - self.r))

            manager_loss = .5 * v_loss_m + gt_loss
            

            grads_m = tf.gradients(manager_loss, pi.var_list_m, stop_gradients=pi.var_list_w_only) # TODO: var list!!!!
            grads_m, _ = tf.clip_by_global_norm(grads_m, 40.0)

            
            # TODO: gradient for worker policy

            log_prob_tf = tf.nn.log_softmax(pi.logits) # TODO: logits isn't correct
            prob_tf = tf.nn.softmax(pi.logits) # TODO: logits isn't correct

            # TODO: how do you calculate value loss for the worker? (since it's based on both env reward and intrinsic reward)

            pi_loss = -tf.reduce_sum(tf.reduce_sum(log_prob_tf * self.ac, [1]) * self.adv_w)
            v_loss_w = .5 * tf.reduce_sum(tf.square(pi.w_vf - self.r))

            # TODO: do I still use entropy? I assume no?
            bs = tf.to_float(tf.shape(pi.x)[0])
            print(tf.shape(pi.x))

            entropy = - tf.reduce_sum(prob_tf * log_prob_tf)
            
            worker_loss = .5 * v_loss_w + pi_loss - entropy*.01

            grads_w = tf.gradients(worker_loss, pi.var_list_w, stop_gradients=pi.var_list_m_only)  # TODO: var list!!!
            grads_w, _ = tf.clip_by_global_norm(grads_w, 40.0)

            # TODO: logging/summaries
            #tf.summary.scalar("model/epsilon", tf.reduce_sum(self.epsilon))
            tf.summary.scalar("model/reward", tf.reduce_sum(self.r))
            tf.summary.scalar("model/adv_m", tf.reduce_sum(self.adv_m))
            tf.summary.scalar("model/adv_w", tf.reduce_sum(self.adv_w))
            tf.summary.scalar("model/r_intrins", tf.reduce_sum(self.r_intrinsic))
            tf.summary.scalar("model/g_dist", tf.reduce_sum(self.g_dist))
            tf.summary.scalar("model/s_diff", tf.reduce_sum(self.s_diff))
            tf.summary.scalar("model/g_hist", tf.reduce_sum(self.g_hist))
            tf.summary.scalar("model/policy_loss", pi_loss / bs)
            tf.summary.scalar("model/worker_value_loss", v_loss_w / bs)
            tf.summary.scalar("model/manager_value_loss", v_loss_m / bs)
            tf.summary.scalar("model/manager_loss", manager_loss / bs)
            tf.summary.scalar("model/worker_loss", worker_loss / bs)
            tf.summary.scalar("model/entropy", entropy / bs)
            #tf.summary.scalar("model/entropy", entropy / bs)
            tf.summary.image("model/state", pi.x)
            #tf.summary.scalar("model/grad_global_norm", tf.global_norm(grads))
            #tf.summary.scalar("model/var_global_norm", tf.global_norm(pi.var_list))


            tf.summary.scalar("model_inner/w_vf", tf.reduce_sum(pi.w_vf))
            tf.summary.histogram("model_inner/z", pi.z)
            tf.summary.histogram("model_inner/z_alt", pi.z_alt)
            tf.summary.histogram("model_inner/U", pi.U)
            tf.summary.histogram("model_inner/w_c_in", pi.w_c_in)
            tf.summary.histogram("model_inner/w_h_in", pi.w_h_in)
            tf.summary.scalar("model_inner/grads_w", tf.global_norm(grads_w))
            tf.summary.scalar("model_inner/grads_m", tf.global_norm(grads_m))
            #tf.summary.histogram("model_inner/w_lstm_outputs", pi.w_lstm_outputs_debug)
            
            self.summary_op = tf.summary.merge_all()

            self.runner = RunnerThread(env, pi, LOCAL_STEPS, visualise, renderOnly)


            # copy weights from param server to local model
            self.sync_m = tf.group(*[v1.assign(v2) for v1, v2 in zip(pi.var_list_m, self.network.var_list_m)])
            self.sync_w = tf.group(*[v1.assign(v2) for v1, v2 in zip(pi.var_list_w, self.network.var_list_w)])

            #print("MANAGER:")
            #for v1, v2 in zip(pi.var_list_m, self.network.var_list_m):
                #print("---- ", v1, v2)
            #print("WORKER:")
            #for v1, v2 in zip(pi.var_list_w, self.network.var_list_w):
                #print("---- ", v1, v2)
                

            self.sync = tf.group(self.sync_m, self.sync_w)
            #print("Sync M:", self.sync_m)
            #print("Sync W:", self.sync_w)
            #print("Self sync:", self.sync)

            grads_and_vars_m = list(zip(grads_m, self.network.var_list_m))
            grads_and_vars_w = list(zip(grads_w, self.network.var_list_w))
            inc_step = self.global_step.assign_add(tf.shape(pi.x)[0])
            

            opt_m = tf.train.RMSPropOptimizer(LEARNING_RATE, ALPHA, use_locking=True)
            opt_w = tf.train.RMSPropOptimizer(LEARNING_RATE, ALPHA, use_locking=True)
            #opt_m = tf.train.AdamOptimizer(LEARNING_RATE)
            #opt_w = tf.train.AdamOptimizer(LEARNING_RATE)

            self.train_op = tf.group(opt_m.apply_gradients(grads_and_vars_m), opt_w.apply_gradients(grads_and_vars_w), inc_step)
            self.summary_writer = None
            self.local_steps = 0




            '''
            self.ac = tf.placeholder(tf.float32, [None, env.action_space.n], name="ac")
            self.adv = tf.placeholder(tf.float32, [None], name="adv")
            self.r = tf.placeholder(tf.float32, [None], name="r")

            log_prob_tf = tf.nn.log_softmax(pi.logits)
            prob_tf = tf.nn.softmax(pi.logits)

            # the "policy gradients" loss:  its derivative is precisely the policy gradient
            # notice that self.ac is a placeholder that is provided externally.
            # adv will contain the advantages, as calculated in process_rollout
            pi_loss = - tf.reduce_sum(tf.reduce_sum(log_prob_tf * self.ac, [1]) * self.adv)

            # loss of value function
            vf_loss = 0.5 * tf.reduce_sum(tf.square(pi.vf - self.r))
            entropy = - tf.reduce_sum(prob_tf * log_prob_tf)

            bs = tf.to_float(tf.shape(pi.x)[0])
            self.loss = pi_loss + 0.5 * vf_loss - entropy * 0.01

            # 20 represents the number of "local steps":  the number of timesteps
            # we run the policy before we update the parameters.
            # The larger local steps is, the lower is the variance in our policy gradients estimate
            # on the one hand;  but on the other hand, we get less frequent parameter updates, which
            # slows down learning.  In this code, we found that making local steps be much
            # smaller than 20 makes the algorithm more difficult to tune and to get to work.
            self.runner = RunnerThread(env, pi, LOCAL_STEPS, visualise, renderOnly)


            grads = tf.gradients(self.loss, pi.var_list)

            if use_tf12_api:
                tf.summary.scalar("model/policy_loss", pi_loss / bs)
                tf.summary.scalar("model/value_loss", vf_loss / bs)
                tf.summary.scalar("model/entropy", entropy / bs)
                tf.summary.image("model/state", pi.x)
                tf.summary.scalar("model/grad_global_norm", tf.global_norm(grads))
                tf.summary.scalar("model/var_global_norm", tf.global_norm(pi.var_list))
                self.summary_op = tf.summary.merge_all()

            else:
                tf.scalar_summary("model/policy_loss", pi_loss / bs)
                tf.scalar_summary("model/value_loss", vf_loss / bs)
                tf.scalar_summary("model/entropy", entropy / bs)
                tf.image_summary("model/state", pi.x)
                tf.scalar_summary("model/grad_global_norm", tf.global_norm(grads))
                tf.scalar_summary("model/var_global_norm", tf.global_norm(pi.var_list))
                self.summary_op = tf.merge_all_summaries()

            grads, _ = tf.clip_by_global_norm(grads, 40.0)

            # copy weights from the parameter server to the local model
            self.sync = tf.group(*[v1.assign(v2) for v1, v2 in zip(pi.var_list, self.network.var_list)])

            grads_and_vars = list(zip(grads, self.network.var_list))
            inc_step = self.global_step.assign_add(tf.shape(pi.x)[0])

            # each worker has a different set of adam optimizer parameters
            #opt = tf.train.AdamOptimizer(1e-4)
            opt = tf.train.RMSPropOptimizer(LEARNING_RATE, ALPHA, use_locking=True)
            self.train_op = tf.group(opt.apply_gradients(grads_and_vars), inc_step)
            self.summary_writer = None
            self.local_steps = 0
            '''

    def start(self, sess, summary_writer):
        try:
            sess.run(tf.global_variables_initializer()) # TODO: recently added, dunno if it belongs
        except: pass
        self.runner.start_runner(sess, summary_writer)
        self.summary_writer = summary_writer

    def pull_batch_from_queue(self):
        """
self explanatory:  take a rollout from the queue of the thread runner.
"""
        rollout = self.runner.queue.get(timeout=600.0)
        while not rollout.terminal:
            try:
                rollout.extend(self.runner.queue.get_nowait())
            except queue.Empty:
                break
        return rollout

    def process(self, sess):
        """
process grabs a rollout that's been produced by the thread runner,
and updates the parameters.  The update is then sent to the parameter
server.
"""
        global EPSILON

        #print("hello from process")
        #sys.stdout.flush()

        sess.run(self.sync)  # copy weights from shared to local # TODO: TODO: TODO: TODO: this is what's causing NaN's
        
        #print("synced")
        sys.stdout.flush()
        
        rollout = self.pull_batch_from_queue()
        batch = process_rollout(rollout, sess, gamma=0.99, lambda_=1.0)
        if batch == -1: return

        # get intrinsic rewards
        intrinsic_reward = sess.run(self.r_intrinsic, feed_dict={
            self.g_hist: batch.g_hist,
            self.s_diff: batch.s_diff})
            #self.gt: batch.gt,
            #self.latent_states: batch.latent_states})


        # calculate worker advantage
        pred_v_w =np.asarray(rollout.values_w + [rollout.r])
        delta_t_w_intrins = np.asarray(rollout.rewards) + INTRINSIC_INFLUENCE*intrinsic_reward + WORKER_DISCOUNT*pred_v_w[1:] - pred_v_w[:-1]
        batch_adv_w = discount(delta_t_w_intrins, WORKER_DISCOUNT)
        
        #print("features_w_c", batch.features_w[0])
        #print(batch.features_w[0])
        #print("features_w_h", batch.features_w[1])
        #print(batch.features_w[1])

        # TODO: TODO: TODO: TODO: don't compute summaries every time!
        should_compute_summary = self.task == 0 and self.local_steps % 12 == 0
        #should_compute_summary = True

        if should_compute_summary:
            fetches = [self.summary_op, self.train_op, self.global_step]
            #fetches = [self.summary_op, self.global_step]
        else:
            fetches = [self.train_op, self.global_step]
            #fetches = [self.global_step]

        feed_dict = {
                self.local_network.x: batch.si, 
                self.r: batch.r,
                self.ac: batch.ac,
                self.v_w: batch.v_w,
                self.adv_m: batch.adv_m,
                self.adv_w: batch_adv_w,
                self.gt: batch.gt,
                self.g_hist: batch.g_hist,
                self.latent_states: batch.latent_states,
                self.s_diff: batch.s_diff,
                #self.epsilon: EPSILON*batch.s_diff.shape[0],
                self.local_network.m_state_in[0]: batch.features_m[0],
                self.local_network.m_state_in[1]: batch.features_m[1],
                self.local_network.w_state_in[0]: batch.features_w[0],
                self.local_network.w_state_in[1]: batch.features_w[1]
        }


        '''
        feed_dict = {
            self.local_network.x: batch.si,
            self.ac: batch.a,
            self.adv: batch.adv,
            self.r: batch.r,
            self.local_network.state_in[0]: batch.features[0],
            self.local_network.state_in[1]: batch.features[1],
        }
        '''

        fetched = sess.run(fetches, feed_dict=feed_dict)

        if should_compute_summary:
            self.summary_writer.add_summary(tf.Summary.FromString(fetched[0]), fetched[-1])
            self.summary_writer.flush()
        self.local_steps += 1
