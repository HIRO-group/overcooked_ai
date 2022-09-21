from arguments import get_arguments
from agent import OAIAgent, OAITrainer
from networks import GridEncoder, MLP, weights_init_, get_output_shape
from overcooked_ai_py.mdp.overcooked_mdp import Action
from overcooked_ai_py.visualization.state_visualizer import StateVisualizer
from overcooked_dataset import OvercookedDataset
from overcooked_gym_env import OvercookedGymEnv
from state_encodings import ENCODING_SCHEMES

from copy import deepcopy
import numpy as np
from pathlib import Path
import pygame
from pygame.locals import HWSURFACE, DOUBLEBUF, RESIZABLE, QUIT, VIDEORESIZE
from tqdm import tqdm
import time
import torch as th
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.distributions.categorical import Categorical
from typing import Dict, Any
import wandb


class BehaviouralCloningPolicy(nn.Module):
    def __init__(self, visual_obs_shape, agent_obs_shape, args, act=nn.ReLU, hidden_dim=256):
        """
        NN network for a behavioral cloning agent
        :param visual_obs_shape: Shape of any grid-like input to be passed into a CNN
        :param agent_obs_shape: Shape of any vector input to passed only into an MLP
        :param depth: Depth of CNN
        :param act: activation function
        :param hidden_dim: hidden dimension to use in NNs
        """
        super(BehaviouralCloningPolicy, self).__init__()
        self.device = args.device
        self.use_visual_obs = np.prod(visual_obs_shape) > 0
        self.use_agent_obs = np.prod(agent_obs_shape) > 0

        # Define CNN for grid-like observations
        if self.use_visual_obs:
            self.cnn = GridEncoder(visual_obs_shape)
            self.cnn_output_shape = get_output_shape(self.cnn, [1, *visual_obs_shape])[0]
        else:
            self.cnn_output_shape = 0

        # Define MLP for vector/feature based observations
        self.mlp = MLP(input_dim=self.cnn_output_shape + np.prod(agent_obs_shape),
                       output_dim=hidden_dim, hidden_dim=hidden_dim, act=act)
        self.action_predictor = nn.Linear(hidden_dim, Action.NUM_ACTIONS)

        self.apply(weights_init_)
        self.to(self.device)

    def get_latent_feats(self, obs):
        mlp_input = []



        # Concatenate all input features before passing them to MLP
        if self.use_visual_obs:
            # Add batch dim, avoids broadcasting errors down the line
            if len(obs['visual_obs'].shape) == 3:
                obs['visual_obs'] = obs['visual_obs'].unsqueeze(0)
            mlp_input.append(self.cnn.forward(obs['visual_obs']))
        if self.use_agent_obs:
            # Add batch dim, avoids broadcasting errors down the line
            if len(obs['agent_obs'].shape) == 3:
                obs['agent_obs'] = obs['agent_obs'].unsqueeze(0)
            mlp_input.append(obs['agent_obs'])
        return self.mlp.forward(th.cat(mlp_input, dim=-1))

    def forward(self, obs):
        return self.action_predictor(self.get_latent_feats(obs))

    def predict(self, obs, state=None, episode_start=None, deterministic=False):
        """Predict action. If sample is True, sample action from distribution, else pick best scoring action"""
        return Categorical(logits=self.forward(obs)).sample() if deterministic else th.argmax(self.forward(obs), dim=-1), None

    def get_distribution(self, obs):
        return Categorical(logits=self.forward(obs))


class BehaviouralCloningAgent(OAIAgent):
    def __init__(self, visual_obs_shape, agent_obs_shape, args, hidden_dim=256, name=None):
        super(BehaviouralCloningAgent, self).__init__('bc', args)
        self.visual_obs_shape, self.agent_obs_shape, self.args, self.hidden_dim = \
             visual_obs_shape, agent_obs_shape, args, hidden_dim
        self.device = args.device
        self.policy = BehaviouralCloningPolicy(visual_obs_shape, agent_obs_shape, args, hidden_dim=hidden_dim)
        self.to(self.device)
        self.num_timesteps = 0

    def _get_constructor_parameters(self) -> Dict[str, Any]:
        """
        Get data that need to be saved in order to re-create the model when loading it from disk.
        :return: The dictionary to pass to the as kwargs constructor when reconstruction this model.
        """
        return dict(
            visual_obs_shape=self.visual_obs_shape,
            agent_obs_shape=self.agent_obs_shape,
            hidden_dim = self.hidden_dim
        )

    def forward(self, obs):
        z = self.policy.get_latent_feats(obs)
        return self.policy.action_predictor(z)

    def predict(self, obs, state=None, episode_start=None, deterministic=False):
        obs = {k: th.tensor(v, device=self.device) for k, v in obs.items()}
        action_logits = self.forward(obs)
        action = Categorical(logits=action_logits).sample() if deterministic else th.argmax(action_logits, dim=-1)
        return action, None

    def get_distribution(self, obs: th.Tensor):
        obs = {k: th.tensor(v, device=self.device).unsqueeze(0) for k, v in obs.items()}
        return self.policy.get_distribution(obs)

# TODO clean up and remove p_idx
class BehavioralCloningTrainer(OAITrainer):
    def __init__(self, dataset, args, vis_eval=False):
        """
        Class to train BC agent
        :param env: Overcooked environment to use
        :param dataset: That dataset to train on - can be None if the only visualizing agetns
        :param args: arguments to use
        :param vis_eval: If true, the evaluate function will visualize the agents
        """
        super(BehavioralCloningTrainer, self).__init__('bc', args)
        self.device = th.device('cuda' if th.cuda.is_available() else 'cpu')
        self.num_players = 2
        self.dataset = dataset
        self.train_dataset = OvercookedDataset(dataset, [args.layout_name], args)
        self.grid_shape = self.train_dataset.grid_shape
        self.eval_env = OvercookedGymEnv(shape_rewards=False, grid_shape=self.grid_shape, args=args)
        obs = self.eval_env.get_obs()
        visual_obs_shape = obs['visual_obs'][0].shape if 'visual_obs' in obs else 0
        agent_obs_shape = obs['agent_obs'][0].shape if 'agent_obs' in obs else 0
        self.agent = BehaviouralCloningAgent(visual_obs_shape, agent_obs_shape, args)
        self.agents = [self.agent]
        self.optimizer = th.optim.Adam(self.agent.parameters(), lr=args.lr)
        action_weights = th.tensor(self.train_dataset.get_action_weights(), dtype=th.float32, device=self.device)
        self.action_criterion = nn.CrossEntropyLoss(weight=action_weights)
        if vis_eval:
            self.eval_env.setup_visualization()

    def train_on_batch(self, batch):
        """Train BC agent on a batch of data"""
        batch = {k: v.to(self.device) for k, v in batch.items()}
        action = batch['joint_action'].long()
        losses = []
        # train agent on both players actions
        for i in range(self.num_players):
            self.optimizer.zero_grad()
            obs = {}
            if 'visual_obs' in batch:
                obs['visual_obs'] = batch['visual_obs'][:, i]
            if 'agent_obs' in batch:
                obs['agent_obs'] = batch['agent_obs'][:, i]
            preds = self.agent.forward(obs)
            # Train on action prediction task
            loss = self.action_criterion(preds, action[:, i])
            loss.backward()
            self.optimizer.step()
            losses.append(loss.item())
            self.agent.num_timesteps += self.args.batch_size
        return losses

    def train_epoch(self):
        self.agent.train()
        losses = []
        dataloader = DataLoader(self.train_dataset, batch_size=self.args.batch_size, shuffle=True, num_workers=4)
        for batch in tqdm(dataloader):
            losses += self.train_on_batch(batch)
        return np.mean(losses)

    def train_agents(self, epochs=100, exp_name=None):
        """ Training routine """
        exp_name = exp_name or self.args.exp_name
        run = wandb.init(project="overcooked_ai_test", entity=self.args.wandb_ent, dir=str(self.args.base_dir / 'wandb'),
                         reinit=True, name='_'.join([exp_name, self.args.layout_name, 'bc']),
                         mode=self.args.wandb_mode)

        best_reward, best_path, best_tag = 0, None, None
        for epoch in range(epochs):
            mean_loss = self.train_epoch()
            if epoch % 10 == 0:
                mean_reward = self.evaluate(self.agent, self.agent, timestep=epoch)
                wandb.log({'mean_loss': mean_loss, 'epoch': epoch})
                if mean_reward > best_reward:
                    best_path, best_tag = self.save_agents()
                    best_reward = mean_reward
        if best_path is not None:
            self.load_agents(best_path, best_tag)
        run.finish()


if __name__ == '__main__':
    args = get_arguments()
    eval_only = False
    if eval_only:
        bct = BehavioralCloningTrainer('tf_test_5_5.2.pickle', args, vis_eval=True)
        bct.evaluate(10)
    else:
        args.batch_size = 4
        args.layout_name = 'tf_test_5_5'
        bct = BehavioralCloningTrainer('tf_test_5_5.2.pickle', args, vis_eval=True)