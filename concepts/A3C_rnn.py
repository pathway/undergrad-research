import tensorflow as tf
from tensorflow.contrib import slim
import gym
import numpy as np

from skimage.transform import resize
from skimage.color import rgb2grey
from skimage.io import imsave

import random
import time

import subprocess
import multiprocessing
import threading
#from Queue import Queue

import scipy
import scipy.signal


# https://jaromiru.com/2017/02/16/lets-make-an-a3c-theory/ 
# https://jaromiru.com/2017/03/26/lets-make-an-a3c-implementation/ 
# https://github.com/jaara/AI-blog/blob/master/CartPole-A3C.py
# https://medium.com/emergent-future/simple-reinforcement-learning-with-tensorflow-part-8-asynchronous-actor-critic-agents-a3c-c88f72a5e9f2 
# https://medium.com/@henrymao/reinforcement-learning-using-asynchronous-advantage-actor-critic-704147f91686 
# https://github.com/mrahtz/tensorflow-a3c/blob/master/network.py
# https://github.com/openai/universe-starter-agent/blob/master/a3c.py 


# https://cgnicholls.github.io/reinforcement-learning/2017/03/27/a3c.html 


# returns a set of operations to set all weights of destination scope to values of weights from source scope
def getWeightChangeOps(scopeSrc, scopeDest):
    srcVars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scopeSrc)
    destVars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scopeDest)

    assignOps = []
    for srcVar, destVar in zip(srcVars, destVars):
        #assignOps.append(tf.assign(srcVar, destVar)) # NOTE: this is freaking backwards!!!!!!
        assignOps.append(tf.assign(destVar, srcVar))

    return assignOps

# calculates discounted return TODO: figure out why this actually works?
def discount(x, gamma):
    return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]


def normalized_columns_initializer(std=1.0):
    def _initializer(shape, dtype=None, partition_info=None):
        out = np.random.randn(*shape).astype(np.float32)
        out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
        return tf.constant(out)
    return _initializer


# globals NOTE: caution!
atariEnvFree = True
T = 0
global_net = None
global_index = 0


# hyperparameters
#GAME = "SpaceInvaders-v0"
GAME = "PongDeterministic-v3"
ACTION_SIZE = 6

ACTION_REPEAT = 1
STATE_FRAME_COUNT = 1

LEARNING_RATE = .0001
#LEARNING_RATE = .001
NUM_WORKERS = 16
#NUM_WORKERS = 1


t_MAX = 5
T_MAX = 40000 # (epoch training steps)
#T_MAX = 1000 # (epoch training steps)

GAMMA = .99
BETA = .01
ALPHA = .99 # rmsprop decay

TEST_RUN_COUNT = 5

EPOCHS = 1000


class Manager:

    def __init__(self):
        global global_net

        self.optimizer = tf.train.RMSPropOptimizer(LEARNING_RATE, ALPHA, use_locking=True)
        self.globalNetwork = Network('global', self.optimizer)
        self.globalNetwork.buildGraph()
        global_net = self.globalNetwork
        merged_summaries = tf.summary.merge_all()
        self.session = tf.Session()
        self.train_writer = tf.summary.FileWriter('../tensorboard_data/a3c_' + GAME, self.session.graph)
        
        
    def buildWorkers(self):
        print("Number of threads: ", NUM_WORKERS)
        self.workers = []
        for i in range(NUM_WORKERS):
            self.workers.append(Worker("worker_" + str(i), self.optimizer))
        
        self.session.run(tf.global_variables_initializer())


    def runEpoch(self, epochNum):
        coordinator = tf.train.Coordinator()
        #sess.run(tf.global_variables_initializer())
    
        # logging things

        # create worker threads
        worker_threads = []
        for worker in self.workers:
            worker_function = lambda: worker.work(self.session, coordinator, self.train_writer)
            t = threading.Thread(target=worker_function)
            t.start()
            worker_threads.append(t)
            
        self.train_writer.add_graph(self.session.graph)

        coordinator.join(worker_threads)

    def singleTestRun(self, epochNum, render=False):
        e = Environment()
        if render: e.env = gym.wrappers.Monitor(e.env, './runs/epoch_' + str(epochNum), force=True)
        state = e.getInitialState()
        terminal = False
        rnn_state = self.globalNetwork.state_init
        while not terminal:
            policyVec, rnn_state = self.session.run([self.globalNetwork.policy_out, self.globalNetwork.state_out], feed_dict={self.globalNetwork.input: [state], self.globalNetwork.state_in[0]: rnn_state[0], self.globalNetwork.state_in[1]: rnn_state[1]})
            action = np.argmax(policyVec)
            state, reward, terminal = e.act(action)
                    
        print("FINAL SCORE:",e.finalScore)
        return e.finalScore

                    
        
    def testGlobal(self, epochNum):
        #sess.run(tf.global_variables_initializer())

        scores = []

        for i in range(TEST_RUN_COUNT):
            #score = self.singleTestRun()
            if i == 0: score = self.singleTestRun(epochNum, True)
            else: score = self.singleTestRun(epochNum)
            scores.append(score)
            
        #avgScore = np.average(scores)
        
        score_log = self.session.run([self.globalNetwork.log_score], feed_dict={self.globalNetwork.score: scores})
        self.train_writer.add_summary(score_log[0], epochNum)

    def run(self):
        global T

        self.buildWorkers()
        for i in range(EPOCHS):
            T = 0
            self.runEpoch(i)
            self.testGlobal(i)
            subprocess.call(['notify-send', "Epoch " + str(i) + " complete"])


class Worker:

    def __init__(self, name, optimizer):
        self.name = name
        #self.optimizer = optimizer

        self.index = 0
        
        self.network = Network(self.name, optimizer)
        self.network.buildGraph()

        self.resetWeights = getWeightChangeOps("global", self.name)

        print("Worker",self.name,"initialized...")

    def train(self, history, session, bootstrap):
        history = np.array(history)
        states = history[:,0]
        actions = history[:,1]
        rewards = history[:,2]
        states_next = history[:,3]
        values = history[:,4]
        #print("Passed rewards:", rewards)
        #print("Passed values:", values)


        actions = np.stack(actions)



        #rewards = np.asarray(rewards)
        #values = np.asarray(values + [bootstrap]) # vpred_t
        #rewards_plus = np.asarray(rewards + [bootstrap]) # rewards_plus_v
        #discountedRewards = discount(rewards_plus, GAMMA)[:-1] # batch_r
        #advantages = rewards[1:] + GAMMA*values[1:] - values[:-1] # delta_t
        #advantages = discount(advantages, GAMMA)

        


        values = np.asarray(values.tolist() + [bootstrap]) # TODO: figure out what the bootstrapping stuff is?
        #print("########### VALUES ###############")
        #print(values)
        rewards_plus = np.asarray(rewards.tolist() + [bootstrap]) # TODO: figure out what the bootstrapping stuff is?
        #print("rewards:",rewards.shape)
        discountedRewards = discount(rewards_plus, GAMMA)[:-1]
        #print("########### REWARDS ############### (target_v)")
        #print(discountedRewards)
        #print(discountedRewards)
        #print("rewards:",rewards.shape)
        #print("values:",values[1:].shape)

        # NOTE: values[1:] = the next state, values[:-1] = the previous state
        # A = Q - V(s)
        # Q = r + yV(s')
        # A = r + yV(s') - V(S)
        #print("values:",values[1:].shape)
        #print("values:",values[:-1].shape)
        advantages = rewards + GAMMA*values[1:] - values[:-1]
        #print("advnatages:",advantages.shape)

        # TODO: supposedly we have to discount advantages, I don't know if that is correct or not (shouldn't we just use discounted rewards?)
        advantages = discount(advantages, GAMMA) # NOTE: wasn't previously commented out

        #print(history.shape)
        #print(states.shape)
        states = np.asarray(states)
        states = np.stack(states, 0)
        #states = np.dstack(states)
        #states = np.array(np.split(states, 3))
        #states = np.split(states, 1)
        #print(states.shape)





        


        # apply gradients to global network
        rnn_state = self.network.state_init
        summary, p_loss, v_loss, val, _ = session.run([self.network.log_op, self.network.policy_loss, self.network.value_loss, self.network.value_out, self.network.apply_gradients], feed_dict={
            self.network.input: states, 
            self.network.actions: actions,
            self.network.target_v: discountedRewards,
            self.network.advantages: advantages,
            self.network.state_in[0]: rnn_state[0],
            self.network.state_in[1]: rnn_state[1]
            })

        #print("intermediate:")
        #print(test)

        #print("val_out:")
        #print(val)
        
        # NOTE: this is the actual one
        #summary, p_loss, v_loss, _ = session.run([self.network.log_op, self.network.policy_loss, self.network.value_loss, self.network.apply_gradients], feed_dict={self.network.input: states, self.network.actions: actions, self.network.target_v: discountedRewards, self.network.advantages: advantages})


        
        #p_loss, v_loss, _ = session.run([self.network.policy_loss, self.network.value_loss, self.network.apply_gradients], feed_dict={self.network.input: states, self.network.actions: actions, self.network.target_v: discountedRewards, self.network.advantages: advantages})
        #p_loss, v_loss = session.run([self.network.policy_loss, self.network.value_loss], feed_dict={self.network.input: states, self.network.actions: actions, self.network.target_v: discountedRewards, self.network.advantages: advantages})

        #print("Policy loss:",p_loss,"Value loss:",v_loss)
        return summary, p_loss, v_loss
        #return p_loss, v_loss

        
    
    def work(self, session, coordinator, train_writer):
        t = 0
        #T = 0
        global T
        global global_index

        episodeCount = 0
        
        while not coordinator.should_stop():


            episode_reward = 0.0
            episode_values = []

            # reset ops
            session.run(self.resetWeights)

            # get an environment instance
            #time.sleep(random.uniform(0.0,0.5))
            self.env = Environment()

            history = []

            t_start = t

            # get state s_t
            s_t = self.env.getInitialState()
            terminal = False


            rnn_state = self.network.state_init

            # repeat until terminal state
            while not terminal:
                # perform a_t according to policY9a_t|s_t; theta_)
                #policyVec, v = session.run([self.network.policy_out, self.network.value_out], feed_dict={self.network.input: [s_t]})
                #a_t = np.argmax(policyVec)
                


               # a_t = np.random.choice(ACTION_SIZE, p=policyVec[0])


                a_t_hot, v, rnn_state = session.run([self.network.sample, self.network.value_out, self.network.state_out], feed_dict={self.network.input: [s_t], self.network.state_in[0]: rnn_state[0], self.network.state_in[1]: rnn_state[1] })
                #print(a_t)
                a_t = np.argmax(a_t_hot)
                #a_dist, v, rnn_state = session.run([self.network.policy_out, self.network.value_out, self.network.state_out], feed_dict={self.network.input: [s_t], self.network.state_in[0]: rnn_state[0], self.network.state_in[1]: rnn_state[1] })
                #a_t = np.random.choice(a_dist[0], p=a_dist[0])
                #a_t = np.argmax(a_dist == a_t)
               
                

                #print(self.name," - value prediction - ",v[0])

                #if self.name == "worker_0":
                    #self.env.env.render()

                # receive reward r_t and new state s_{t+1}
                #a_t = a.act(s_t)
                s_t1, r_t, terminal = self.env.act(a_t)
                #if (r_t != 0): print(self.name,"*********************** got a reward",r_t)

                #history.append([s_t, a_t, r_t, s_t1, v[0,0]])
                #history.append([s_t, a_t, r_t, s_t1, v[0]])
                history.append([s_t, a_t_hot, r_t, s_t1, v[0]])
                #episode_values.append(v[0,0])

                s_t = s_t1

                t += 1
                T += 1

                if terminal or t - t_start >= t_MAX:
                    #print("====================================== training")
                    
                    states = np.array(history)[:,0]
                    states = np.asarray(states)
                    states = np.stack(states, 0)
                    
                    # NOTE: LOGGING
                    #global_weights = session.run([global_net.value_w], feed_dict={global_net.input: states})
                    #local_weights = session.run([self.network.value_w], feed_dict={self.network.input: states})
                    #print("----- BEFORE TRAIN -----")
                    #print("Global weights:")
                    #print(global_weights[0][0])
                    #print("Local weights:")
                    #print(local_weights[0][0])

                    
                    R = 0.0
                    #if not terminal: R = session.run([self.network.value_out], feed_dict={self.network.input:[s_t]})[0]
                    
                    if not terminal: 
                        R = session.run([self.network.value_out], feed_dict={
                            self.network.input:[s_t], 
                            self.network.state_in[0]: rnn_state[0],
                            self.network.state_in[1]: rnn_state[1]})[0]
                        #print(R)
                    else:
                        result = session.run(self.network.log_episode_reward, feed_dict={self.network.episode_reward: [self.env.finalScore]})
                        train_writer.add_summary(result, global_index)
                    
                    summary, p_loss, v_loss = self.train(history, session, R)
                    self.index += 1
                    train_writer.add_summary(summary, self.index)
                    #p_loss, v_loss = self.train(history, session, 0.0, merged_summaries)
                    print(self.name,"[" + str(T) + "]","- Policy loss:",p_loss,"Value loss:",v_loss)

                    # NOTE: LOGGING
                    #global_weights = session.run([global_net.value_w], feed_dict={global_net.input: states})
                    #local_weights = session.run([self.network.value_w], feed_dict={self.network.input: states})
                    #print("----- BEFORE RESET -----")
                    #print("Global weights:")
                    #print(global_weights[0][0])
                    #print("Local weights:")
                    #print(local_weights[0][0])
                    
                    session.run(self.resetWeights)

                    # NOTE: LOGGING
                    #global_weights = session.run([global_net.value_w], feed_dict={global_net.input: states})
                    #local_weights = session.run([self.network.value_w], feed_dict={self.network.input: states})
                    #print("----- AFTER RESET -----")
                    #print("Global weights:")
                    #print(global_weights[0][0])
                    #print("Local weights:")
                    #print(local_weights[0][0])
                    
                    history = []
                    t_start = t
                    #print("-------------------------------------- /training")

            #weights, global_summary = session.run([global_net.value_w, global_net.log_weights], feed_dict={global_net.input: states})
            #weights, global_summary = session.run([global_net.value_w, global_net.log_weights], feed_dict={global_net.input: states})
            global_index += 1
            #train_writer.add_summary(global_summary, global_index)
            
            if T > T_MAX: break

class Network:
    def __init__(self, scope, optimizer):
        self.scope = scope
        self.optimizer = optimizer
        
        

    def buildGraph(self):
        print("Building graph with scope", self.scope)
        with tf.variable_scope(self.scope):
            #self.input = tf.placeholder(tf.float32, shape=(1,84,84,4), name='input') # TODO: pretty sure that shape isn't right
            #self.input = tf.placeholder(tf.float32, shape=(None,84,84,4), name='input') # TODO: pretty sure that shape isn't right
            #self.input = tf.placeholder(tf.float32, shape=(None,84,84,1), name='input') 
            self.input = tf.placeholder(tf.float32, shape=(None,42,42,1), name='input') 
            
            # 16 filters, kernel size of 8, stride of 4
            with tf.name_scope('conv1'):
                #self.w1 = tf.Variable(tf.random_normal([8, 8, 4, 16]), name='weights1')
                self.w1 = tf.Variable(tf.random_normal([8, 8, 1, 16]), name='weights1')
                #self.b1 = tf.Variable(tf.random_normal([16]), name='bias1')
                self.b1 = tf.Variable(tf.zeros([16]), name='bias1')
                self.conv1 = tf.nn.conv2d(self.input, self.w1, [1, 4, 4, 1], "VALID", name='conv1') 
                self.conv1_relu = tf.nn.relu(tf.nn.bias_add(self.conv1, self.b1))
                
                self.log_w1 = tf.summary.histogram('w1', self.w1)
                self.log_b1 = tf.summary.histogram('b1', self.b1)
                
            # 32 filters, kernel size of 4, stride of 2
            with tf.name_scope('conv2'):
                self.w2 = tf.Variable(tf.random_normal([4, 4, 16, 32]), name='weights2')
                #self.b2 = tf.Variable(tf.random_normal([32]), name='bias2')
                self.b2 = tf.Variable(tf.zeros([32]), name='bias2')
                self.conv2 = tf.nn.conv2d(self.conv1_relu, self.w2, [1, 2, 2, 1], "VALID", name='conv2') 
                self.conv2_relu = tf.nn.relu(tf.nn.bias_add(self.conv2, self.b2))

                # flattened size is 9*9*32 = 2592
                #self.conv2_out = tf.reshape(self.conv2_relu, [-1, 2592], name='conv2_flatten') 
                self.conv2_out = tf.reshape(self.conv2_relu, [-1, 288], name='conv2_flatten') 
                
                self.log_w2 = tf.summary.histogram('w2', self.w2)
                self.log_b2 = tf.summary.histogram('b2', self.b2)
                

            # fully connected layer with 256 hidden units
            with tf.name_scope('fully_connected'):
                #self.fc_w = tf.Variable(tf.random_normal([2592, 256]), name='fc_weights') 
                self.fc_w = tf.Variable(tf.random_normal([288, 256]), name='fc_weights') 
                #self.fc_b = tf.Variable(tf.random_normal([256]), name='fc_biases') # fully connected biases
                self.fc_b = tf.Variable(tf.zeros([256]), name='fc_biases') # fully connected biases

                self.fc_out = tf.nn.relu_layer(self.conv2_out, self.fc_w, self.fc_b, name='fc_out')

                self.log_fc_w = tf.summary.histogram('fc_w', self.fc_w)
                self.log_fc_b = tf.summary.histogram('fc_b', self.fc_b)




            with tf.name_scope('lstm'):
                lstm_cell = tf.nn.rnn_cell.BasicLSTMCell(256, state_is_tuple=True)
                
                c_init = np.zeros((1, lstm_cell.state_size.c), np.float32)
                h_init = np.zeros((1, lstm_cell.state_size.h), np.float32)
                self.state_init = [c_init, h_init]

                c_in = tf.placeholder(tf.float32, [1, lstm_cell.state_size.c])
                h_in = tf.placeholder(tf.float32, [1, lstm_cell.state_size.h])
                self.state_in = [c_in, h_in]

                
                rnn_in = tf.expand_dims(self.fc_out, [0])
                #rnn_in = self.fc_out
                #step_size = tf.shape(self.input)[:1]
                step_size = tf.shape(self.input)[:1]
                state_in = tf.nn.rnn_cell.LSTMStateTuple(c_in, h_in)
                
                lstm_outputs, lstm_state = tf.nn.dynamic_rnn(lstm_cell, rnn_in, initial_state=state_in, sequence_length=step_size, time_major=False)
                lstm_c, lstm_h = lstm_state
                self.state_out = [lstm_c[:1, :], lstm_h[:1, :]]
                rnn_out = tf.reshape(lstm_outputs, [-1, 256])



            # policy output, policy = distribution of probabilities over actions, use softmax to choose highest probability action
            with tf.name_scope('policy'):
                #self.policy_w = tf.Variable(tf.random_normal([256, ACTION_SIZE]), name='policy_w')
                #self.policy_w = tf.Variable(normalized_columns_initializer(0.01), expected_shape=(256, ACTION_SIZE), name='policy_w')
                #self.policy_b = tf.Variable(tf.constant_initiailizer(0), expected_shape=(256), name='policy_b')

                self.policy_w = tf.get_variable('policy_w', [256, ACTION_SIZE], initializer=normalized_columns_initializer(.01))
                self.policy_b = tf.get_variable('policy_b', [ACTION_SIZE], initializer=tf.constant_initializer(0))

                # TODO: do we need biases as well?
                #self.policy_out = tf.nn.softmax(tf.matmul(self.fc_out, self.policy_w))
                self.policy_out = tf.matmul(rnn_out, self.policy_w) + self.policy_b
                
                self.log_policy_w = tf.summary.histogram('policy_w', self.policy_w)

                #self.policy_out = slim.fully_connected(rnn_out, ACTION_SIZE, activation_fn=tf.nn.softmax, weights_initializer=normalized_columns_initializer(0.01), biases_initializer=None)


            with tf.name_scope('sample'):
                self.sample = tf.one_hot(tf.squeeze(tf.multinomial(self.policy_out - tf.reduce_max(self.policy_out, [1], keep_dims=True), 1), [1]), ACTION_SIZE)[0, :]

                

            # Only a SINGLE output, just a single linear value
            with tf.name_scope('value'):
                #self.value_w = tf.Variable(tf.random_normal([256, 1]), name='value_w')
                #self.value_w = tf.Variable(tf.zeros([256, 1]), name='value_w')

                self.value_w = tf.get_variable('value_w', [256, 1], initializer=normalized_columns_initializer())
                self.value_b = tf.get_variable('value_b', [1], initializer=tf.constant_initializer(0))

                # TODO: do we need a bias for this? (edit: I'm pretty sure since it's a single linear value, there's no point in having a bias value?)

                self.value_out = tf.reshape(tf.matmul(rnn_out, self.value_w) + self.value_b, [-1])

                #self.log_value_w = tf.summary.histogram('value_w', self.value_w)
                
                #self.value_out = slim.fully_connected(rnn_out, 1, activation_fn=None, weights_initializer=normalized_columns_initializer(1.0), biases_initializer=None)

            if self.scope != 'global':
                '''
                self.actions = tf.placeholder(shape=[None], dtype=tf.int32, name='actions')
                self.target_v = tf.placeholder(shape=[None], dtype=tf.float32, name='target_v',)
                self.advantages = tf.placeholder(shape=[None], dtype=tf.float32, name='advantages')
                
                self.actions_onehot = tf.one_hot(self.actions, ACTION_SIZE, dtype=tf.float32)
                self.responsible_outputs = tf.reduce_sum(self.policy_out * self.actions_onehot, [1])
                
                # losses
                self.value_loss = .5 * tf.reduce_sum(tf.square(self.target_v - tf.reshape(self.value_out, [-1])))
                #print(self.target_v.shape,self.value_out.shape)
                # NOTE: calclavia's still does the reshape thing, maybe need to add that
                #self.value_loss = .5 * tf.reduce_sum(tf.square(self.target_v - self.value_out))
                #self.entropy = -tf.reduce_sum(self.policy_out * self.actions_onehot, [1])
                #self.entropy = -tf.reduce_sum(self.policy_out * self.actions_onehot, [1]) # TODO: sign?
                self.entropy = -tf.reduce_sum(self.policy_out * tf.log(tf.clip_by_value(self.policy_out, 1e-20, 1.0))) 
                #self.policy_loss = -tf.reduce_sum(tf.log(self.responsible_outputs)*self.advantages)


                # TODO: do I still really need the stop gradients or not?
                
                #self.policy_loss = -tf.reduce_sum(tf.log(self.responsible_outputs+1e-10)*tf.stop_gradient(self.advantages))
                self.policy_loss = -tf.reduce_sum(tf.log(self.responsible_outputs+1e-10)*self.advantages)
                
                self.loss = .5 * self.value_loss + self.policy_loss - self.entropy * BETA 
                #self.loss = tf.reduce_mean(.5 * self.value_loss + self.policy_loss + self.entropy * BETA) 
                #self.loss = self.value_loss
                '''

                self.actions = tf.placeholder(shape=[None, ACTION_SIZE], dtype=tf.float32, name='actions')
                self.target_v = tf.placeholder(shape=[None], dtype=tf.float32, name='target_v',)
                self.advantages = tf.placeholder(shape=[None], dtype=tf.float32, name='advantages')

                self.log_prob_tf = tf.nn.log_softmax(self.policy_out)
                self.prob_tf = tf.nn.softmax(self.policy_out)

                self.policy_loss = -tf.reduce_sum(tf.reduce_sum(self.log_prob_tf*self.actions, [1])*self.advantages)
                self.value_loss = 0.5 * tf.reduce_sum(tf.square(self.value_out - self.target_v))
                self.entropy = -tf.reduce_sum(self.prob_tf * self.log_prob_tf)

                self.loss = self.policy_loss + .5 * self.value_loss - self.entropy * BETA
                



                # summaries
                self.episode_reward = tf.placeholder(shape=[None], dtype=tf.float32, name='score')
                
                self.log_episode_reward = tf.summary.scalar('episode_reward', tf.reduce_mean(self.episode_reward))
                
                self.log_value_loss = tf.summary.scalar('value_loss', self.value_loss)
                self.log_policy_loss = tf.summary.scalar('policy_loss', self.policy_loss)
                self.log_entropy = tf.summary.scalar('entropy', self.entropy)
                self.log_loss = tf.summary.scalar('loss', tf.reduce_sum(self.loss))
                self.log_state = tf.summary.image('state', self.input)
                #print(self.loss)


                #self.log_op = tf.summary.merge([self.log_value_loss, self.log_policy_loss, self.log_loss])
                #self.log_op = tf.summary.merge([self.log_w1, self.log_b1, self.log_w2, self.log_b2, self.log_fc_w, self.log_fc_b, self.log_value_w, self.log_policy_w, self.log_value_loss, self.log_policy_loss, self.log_loss, self.log_entropy, self.log_state])
                self.log_op = tf.summary.merge([self.log_w1, self.log_b1, self.log_w2, self.log_b2, self.log_fc_w, self.log_fc_b, self.log_value_loss, self.log_policy_loss, self.log_loss, self.log_entropy, self.log_state])
                #self.log_op = tf.summary.merge([self.log_value_loss, self.log_policy_loss])

                local_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, self.scope)
                self.gradients = tf.gradients(self.loss, local_vars)
                self.var_norms = tf.global_norm(local_vars)
                self.clipped_gradients, self.gradient_norms = tf.clip_by_global_norm(self.gradients, 40.0) # TODO: where is 40 coming from???
                
                global_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'global')
                self.apply_gradients = self.optimizer.apply_gradients(zip(self.clipped_gradients, global_vars))
                #self.apply_gradients = self.optimizer.apply_gradients(zip(self.gradients, global_vars))
                #self.apply_gradients = self.optimizer.apply_gradients(zip(self.gradients, local_vars))
                
                
                


            if self.scope == 'global':
                self.score = tf.placeholder(shape=[None], dtype=tf.float32, name='score')
                self.log_score_avg = tf.summary.scalar('score_avg', tf.reduce_mean(self.score))
                self.log_score_min = tf.summary.scalar('score_min', tf.reduce_min(self.score))
                self.log_score_max = tf.summary.scalar('score_max', tf.reduce_max(self.score))
                self.log_score = tf.summary.merge([self.log_score_avg, self.log_score_min, self.log_score_max])
                
                #self.log_weights = tf.summary.merge([self.log_w1, self.log_w2, self.log_fc_w, self.log_value_w, self.log_policy_w])
                #self.merged_summaries = tf.summary.merge_all()
                #self.sess.run(tf.global_variables_initializer())
                
               # self.train_writer = tf.summary.FileWriter('../tensorboard_data/a3c_full' , self.sess.graph)
               # self.train_writer.add_graph(self.sess.graph)




        




class Environment:
    def __init__(self):
        global atariEnvFree
        print("Initializing environment...")

        while not atariEnvFree: time.sleep(.01) # NOTE: some weird thing the atari emulator needs to make sure two threads don't simultaneously create an environment
        atariEnvFree = False
        self.env = gym.make(GAME)
        #self.env = gym.make("Breakout-v0")
        atariEnvFree = True
        
        self.seqSize = STATE_FRAME_COUNT
        self.rawFrameSeq = []
        self.frameSeq = []

        self.lastFrameRaw = None
        self.frame = None

        self.finalScore = 0

        print("Environment initialized")

    def getInitialState(self):
        print("Getting an initial state...")
        frame = self.preprocessFrame(self.env.reset())
        #imsave('INITIAL.png', self.env.reset())
        self.rawFrameSeq.append(frame) # NOTE: need an extra one in case state frame count is zero?

        for i in range(STATE_FRAME_COUNT):
            self.frameSeq.append(frame) # TODO: make this based off of self.seqsize
            self.rawFrameSeq.append(frame)

        state = np.dstack(self.frameSeq)
        
        return state

    def preprocessFrame(self, frame):
        #imsave("unprocessed.png", frame)
        #
        ##frame = resize(frame, (110,84))
        ##frame = resize(frame, (94,84))
        #frame = resize(frame, (84,84))
        ##frame = frame[18:102,0:84]
        ##frame = frame[4:88,0:84]
        ##frame = frame[10:94,0:84]
        #frame = rgb2grey(frame)
        #imsave('processed.png', frame)
        #return frame
    
        frame = frame[34:34+160, :160]
        # Resize by half, then down to 42x42 (essentially mipmapping). If
        # we resize directly we lose pixels that, when mapped to 42x42,
        # aren't close enough to the pixel boundary.
        frame = resize(frame, (80, 80))
        frame = resize(frame, (42, 42))
        frame = frame.mean(2)
        frame = frame.astype(np.float32)
        frame *= (1.0 / 255.0)
        frame = np.reshape(frame, [42, 42, 1])
        return frame


    def act(self, action):

        cumulativeReward = 0.0
        for i in range(ACTION_REPEAT):
            observation, reward, terminal, info = self.env.step(action)
            cumulativeReward += reward
            observationFrame = self.preprocessFrame(observation)
            
            self.rawFrameSeq.pop(0)
            self.rawFrameSeq.append(observationFrame)


            self.frameSeq.pop(0)
            cleanedFrame = np.maximum(self.rawFrameSeq[-1], self.rawFrameSeq[-2])
            #cleanedFrame = np.maximum(self.lastFrameRaw, observationFrame)
            self.frame = cleanedFrame
            #self.lastFrameRaw = observationFrame
            
            #imsave('test.png', cleanedFrame)
            self.frameSeq.append(cleanedFrame)
            
            if terminal: 
                print("TERMINAL STATE REACHED")
                break
            
        state = np.dstack(self.frameSeq)
        #state = self.frame
        
        self.finalScore += cumulativeReward
        
        return state, cumulativeReward, terminal




m = Manager()
m.run()
