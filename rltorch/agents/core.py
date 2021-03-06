from abc import ABC, abstractmethod
import os
import shutil
from abc import abstractmethod
from itertools import chain
from collections import defaultdict, deque
from functools import partial

import numpy as np
from tensorboardX import SummaryWriter
import torch
import torch.optim as optim

from ..memories import ACMemory


class BaseAgent(ABC):
    """Abstract class for Agent

    You need to inherit this class for implementing more detail

    Parameters
    ----------
    state_shape: array-like
        The shape of input
    action_config: dict
        Configuration of action space, which may include type, shape, etc.
    processor: class
        Processor instance to transform input to adjust format of models' input
    smooth_length: int
        The length to smooth data before recording
    log_dir: str
        Directory to store the recrod for tensorboard
    """
    def __init__(self, state_shape, action_config, processor=None, reward_reshape=None,
                 smooth_length=100, log_dir='./logs'):
        super(BaseAgent, self).__init__()
        self.state_shape = state_shape
        self.action_config = action_config
        self.processor = processor
        self.reward_reshape = reward_reshape
        self.smooth_length = smooth_length
        # Delete old logs if any
        if os.path.isdir(log_dir):
            print('Delete old tensorboard log')
            shutil.rmtree(log_dir)
        self.writer = SummaryWriter(log_dir)
        self.record_step = 0
        self.episode_step = 0

    @abstractmethod
    def predict(self, *args, **kwrags):
        raise NotImplementedError

    def record(self, *args, **kwargs):
        pass

    def observe(self, *args, **kwargs):
        pass

    def fit(self, *args, **kwargs):
        pass


class ACAgent(BaseAgent):
    """Actor Critic Base Agent

    sParameters
    ----------
    env: gym.Env
        OpenAI Gym Environment
    state_shape: array-like
        The shape of input
    action_config: dict
        Configuration of action space, which may include type, shape, etc.
    processor: class
        Processor instance to transform input to adjust format of models' input
    smooth_length: int
        The length to smooth data before recording
    log_dir: str
        Directory to store the recrod for tensorboard
    window_length: int
    lr: float
    critic_config: dict
    actor_config: dict
    action_dist: torch.distributions object
        The distribution for action
    discount: float
        Discount Factor
    gae_lambda: float
        GAE parameter for trace eligibility
    num_frames_per_proc: int
    batch_size: int
    entropy_coef: float
    value_loss_coef: float
    max_grad_nrom: float
    """

    def __init__(self, state_shape, action_config, processor,
                 reward_reshape, smooth_length, log_dir,
                 window_length, lr, model_config,
                 action_dist, discount, gae_lambda, num_frames_per_proc,
                 batch_size,
                 entropy_coef, value_loss_coef, max_grad_norm):
        super(ACAgent, self).__init__(state_shape, action_config, processor,
                                      reward_reshape,
                                      smooth_length, log_dir)
        self.widow_length = window_length
        self.action_dist = action_dist
        self.discount = discount
        self.gae_lambda = gae_lambda
        self.num_frames_per_proc = num_frames_per_proc
        self.batch_size = batch_size
        self.entropy_coef = entropy_coef
        self.value_loss_coef = value_loss_coef
        self.max_grad_norm = max_grad_norm
        # Multi Agent Memory
        self.memory = ACMemory(num_frames_per_proc, window_length)
        # Build Network
        self.ac_model = self.build_model(model_config)
        # Build optimizer
        self.parameters = self.ac_model.parameters()
        self.optimizer = optim.Adam(self.parameters, lr=lr)
        # Set device
        self.device = torch.device(
            'cuda' if torch.cuda.is_available() else 'cpu')
        # Record parameters
        self.episode_steps = defaultdict(lambda: 0)
        mydeque = partial(deque, maxlen=self.smooth_length)
        self.reward_record = defaultdict(mydeque)
        self.loss_record = deque(maxlen=self.smooth_length)
        self.actor_loss_record = deque(maxlen=self.smooth_length)
        self.critic_loss_record = deque(maxlen=self.smooth_length)
        self.entropy_record = deque(maxlen=self.smooth_length)
        self.ep_rewards = defaultdict(list)
        self.ep_actions = defaultdict(list)

    @abstractmethod
    def build_model(self, config):
        raise NotImplementedError

    def _calc_dim(self, model):
        x = torch.randn([1] + list(self.state_shape))
        x = model(x)
        return x.size(-1)

    def predict(self, obs, training=True):
        if self.processor is not None:
            obs = [self.processor.process(obs_i) for obs_i in obs]
        state = self.memory.get_recent_state(obs)
        state_tensor = torch.tensor(state, dtype=torch.float,
                                    device=self.device)
        dist, value = self.ac_model(state_tensor)
        action = dist.sample()
        if training:
            log_prob = dist.log_prob(action)
            entropy = dist.entropy()
            self.memory.store_value_log_prob(value, log_prob, entropy)
        return action.cpu().numpy()

    def observe(self, obs, action, reward, terminal, info, training=True):
        obs = [self.processor.process(obs_i) for obs_i in obs]
        self.memory.append(obs, action, reward, terminal, training)
        self.record(action, reward, terminal)

    def record(self, action, reward, terminal):
        n_workers = len(action)
        for i in range(n_workers):
            self.reward_record[i].append(reward[i])
            self.ep_rewards[i].append(reward[i])
            self.ep_actions[i].append(action[i])
            if terminal[i]:
                ep_sum_reward = np.sum(self.ep_rewards[i])
                self.writer.add_scalar(f'data/episode_reward_sum_{i}',
                                       ep_sum_reward,
                                       self.episode_steps[i])

                self.writer.add_histogram(f'data/episode_action_{i}',
                                          np.array(self.ep_actions[i]),
                                          self.episode_steps[i], bins='auto')

                self.writer.add_histogram(f'data/episode_reward_dist_{i}',
                                          np.array(self.ep_rewards[i]),
                                          self.episode_steps[i], bins='auto')

                # Reset record
                self.episode_steps[i] += 1
                self.ep_rewards[i] = []
                self.ep_actions[i] = []
        self.record_step += 1

    def set_new_obs(self, new_obs):
        if self.processor is not None:
            new_obs = [self.processor.process(obs_i) for obs_i in new_obs]
        self.new_obs = new_obs

    def get_newest_state(self):
        return self.memory.get_recent_state(self.new_obs)

    def aggregate_experiences(self):
        experiences = self.memory.sample()
        rewards = torch.tensor(np.array(experiences.reward),
                               dtype=torch.float,
                               device=self.device)
        masks = torch.tensor(1. - np.array(experiences.terminal, dtype=float),
                             device=self.device,
                             dtype=torch.float)
        values = torch.tensor(torch.stack(experiences.value),
                              dtype=torch.float,
                              device=self.device)
        values = torch.sum(values, -1)
        log_probs = torch.tensor(torch.stack(experiences.log_prob),
                                 dtype=torch.float,
                                 device=self.device)

        entropies = torch.tensor(torch.stack(experiences.entropy),
                                 dtype=torch.float,
                                 device=self.device)

        T = len(rewards)
        # Get delta
        deltas = []
        for t in range(T - 1):
            target = (rewards[t] + values[t + 1] * masks[t]).detach()
            delta = target - values[t]
            deltas.append(delta)
        # Estaimte with the newest value
        new_state = torch.tensor(self.get_newest_state(),
                                 dtype=torch.float,
                                 device=self.device)
        new_value = self.ac_model(new_state)[1].sum(-1)
        new_target = (rewards[-1] + new_value * masks[-1]).detach()
        new_delta = new_target - values[-1]
        deltas.append(new_delta)

        # Calculate advantage from deltas
        decay_rate = self.discount * self.gae_lambda
        advs = []
        for t_st in range(T):
            adv = 0.
            power = 0.
            for t in range(t_st, T):
                adv += deltas[t] * (decay_rate ** power)
                power += 1.
            advs.append(adv)
        advs = torch.stack(advs)
        self.memory.reset()
        return advs, log_probs, entropies