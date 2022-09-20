from agent import OAIAgent, SB3Wrapper, OAITrainer
from arguments import get_arguments, get_args_to_save, set_args_from_load
from behavioral_cloning import BehaviouralCloningPolicy, BehaviouralCloningAgent, BehavioralCloningTrainer
from overcooked_gym_env import OvercookedGymEnv
from overcooked_subtask_gym_env import OvercookedSubtaskGymEnv
from overcooked_manager_gym_env import OvercookedManagerGymEnv
from rl_agents import MultipleAgentsTrainer, SingleAgentTrainer, SB3Wrapper, SB3LSTMWrapper, VEC_ENV_CLS
from subtasks import Subtasks, calculate_completed_subtask, get_doable_subtasks
from stable_baselines3.common.env_util import make_vec_env
from state_encodings import ENCODING_SCHEMES

from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld
from overcooked_ai_py.mdp.actions import Action

import numpy as np
from pathlib import Path
import torch as th
from torch.distributions.categorical import Categorical
import torch.nn.functional as F
from typing import Tuple, Union



# TODO Move to util
def is_held_obj(player, object):
    '''Returns True if the object that the "player" picked up / put down is the same as the "object"'''
    x, y = np.array(player.position) + np.array(player.orientation)
    return player.held_object is not None and \
           ((object.name == player.held_object.name) or
            (object.name == 'soup' and player.held_object.name == 'onion'))\
           and object.position == (x, y)


class MultiAgentSubtaskWorker(OAIAgent):
    def __init__(self, agents, args):
        super(MultiAgentSubtaskWorker, self).__init__('multi_agent_subtask_worker', args)
        self.agents = agents

    def predict(self, obs: th.Tensor, state=None, episode_start=None, deterministic: bool=False):
        assert 'curr_subtask' in obs.keys()
        preds = [self.agents[st].predict(obs, state=state, episode_start=episode_start, deterministic=deterministic)
                 for st in obs['curr_subtask']]
        actions, states = zip(*agent_preds)
        return actions, states

    def get_distribution(self, obs: th.Tensor):
        assert 'curr_subtask' in obs.keys()
        return self.agents[obs['curr_subtask']].get_distribution(obs)

    def _get_constructor_parameters(self):
        return dict(name=self.name)

    def save(self, path: str) -> None:
        args = get_args_to_save(self.args)
        agent_path = path + '_subtask_agents_dir'
        Path(agent_path).mkdir(parents=True, exist_ok=True)

        save_dict = {'sb3_model_type': type(self.agents[0]), 'agent_paths': [],
                     'const_params': self._get_constructor_parameters(), 'args': args}
        for i, agent in enumerate(self.agents):
            agent_path_i = agent_path + f'/subtask_{i}_agent'
            agent.save(agent_path_i)

            save_dict['agent_paths'].append(agent_path_i)
        th.save(save_dict, path)

    @classmethod
    def load(cls, path: str, args):
        device = args.device
        saved_variables = th.load(path, map_location=device)
        set_args_from_load(saved_variables['args'], args)
        saved_variables['const_params']['args'] = args

        # Load weights
        agents = []
        for agent_path in saved_variables['agent_paths']:
            agent = saved_variables['sb3_model_type'].load(agent_path, args)
            agent.to(device)
            agents.append(agent)
        return cls(agents=agents, args=args)

    @classmethod
    def create_model_from_scratch(cls, args, dataset_file=None) -> 'OAIAgent':
        if dataset_file is not None:
            bct = BehavioralCloningTrainer(dataset_file, args)
            bct.train_agents(epochs=50)
            tms = bct.get_agents(p_idx=t_idx) # TODO no p_idx or t_idx except in env
        else:
            tsa = MultipleAgentsTrainer(args)
            tsa.train_agents(total_timesteps=1e8)
            tms = tsa.get_agents()

        # Train 12 individual agents, each for a respective subtask
        agents = []
        for i in range(Subtasks.NUM_SUBTASKS):
            # RL single subtask agents trained with BC partner
            kwargs = {'single_subtask_id': i, 'args': args}
            env = make_vec_env(OvercookedSubtaskGymEnv, n_envs=args.n_envs, env_kwargs=env_kwargs, vec_env_cls=VEC_ENV_CLS)
            eval_env = OvercookedSubtaskGymEnv(**kwargs)
            rl_sat = SingleAgentTrainer(tms, args, env=env, eval_env=eval_env)
            if i != Subtasks.SUBTASKS_TO_IDS['unknown']:
                rl_sat.train_agents(total_timesteps=5e6, exp_name=args.exp_name + f'_subtask_{i}')
            agents.append(rl_sat.get_agents())
        model = cls(agents=agents, args=args)
        path = args.base_dir / 'agent_models' / model.name / args.layout_name
        Path(path).mkdir(parents=True, exist_ok=True)
        tag = args.exp_name
        model.save(str(path / tag))
        return model, tms


# Mix-in class
class Manager:
    def update_subtasks(self, completed_subtasks):
        if completed_subtasks == [Subtasks.SUBTASKS_TO_IDS['unknown'], Subtasks.SUBTASKS_TO_IDS['unknown']]:
            self.trajectory = []
        for i in range(2):
            subtask_id = completed_subtasks[i]
            if subtask_id is not None: # i.e. If interact has an effect
                self.worker_subtask_counts[i][subtask_id] += 1

class RLManagerTrainer(SingleAgentTrainer):
    ''' Train an RL agent to play with a provided agent '''
    def __init__(self, worker, teammates, args):
        kwargs = {'worker': worker, 'shape_rewards': True, 'args': args}
        env = make_vec_env(OvercookedManagerGymEnv, n_envs=args.n_envs, env_kwargs=kwargs, vec_env_cls=VEC_ENV_CLS)
        eval_env = OvercookedManagerGymEnv(worker=worker, shape_rewards=False, args=args)
        self.worker = worker
        super(RLManagerTrainer, self).__init__(teammates, args, env=env, eval_env=eval_env)

    def wrap_agent(self, rl_agent):
        if self.use_lstm:
            agent = SB3LSTMWrapper(sb3_agent, f'rl_lstm_manager', self.args)
        else:
            agent = SB3Wrapper(sb3_agent, f'rl_manager', self.args)
        return agent


class HierarchicalRL(OAIAgent):
    def __init__(self, worker, manager, args):
        super(HierarchicalRL, self).__init__('hierarchical_rl', args)
        self.worker = worker
        self.manager = manager

    def get_distribution(self, obs, sample=True):
        if obs['player_completed_subtasks'] is not None:
            # Completed previous subtask, set new subtask
            self.curr_subtask_id = self.manager.predict(obs, sample=sample)[0]
        obs['curr_subtask'] = self.curr_subtask_id
        return self.worker.get_distribution(obs, sample=sample)

    def predict(self, obs, state=None, episode_start=None, deterministic: bool=False):
        if obs['player_completed_subtasks'] is not None:
            # Completed previous subtask, set new subtask
            self.curr_subtask_id = self.manager.predict(obs, state=state, episode_start=episode_start,
                                                        deterministic=deterministic)[0]
        obs['curr_subtask'] = self.curr_subtask_id
        return self.worker.predict(obs, sample=sample)

    def save(self, path: str) -> None:
        """
        Save model to a given location.
        :param path:
        """
        worker_save_path = str(path) + '_worker'
        manager_save_path = str(path) + '_manager'
        self.worker.save(worker_save_path)
        self.manager.save(manager_save_path)
        args = get_args_to_save(self.args)
        th.save({'worker_type': type(self.worker), 'worker_path': worker_save_path,
                 'manager_type': type(self.manager), 'manager_path': manager_save_path,
                 'const_params': self._get_constructor_parameters(), 'args': args},
                       str(path) + '_non_sb3_data')

    @classmethod
    def load(cls, path: str, args) -> 'OAIAgent':
        """
        Load model from path.
        :param path: path to save to
        :param device: Device on which the policy should be loaded.
        :return:
        """
        device = args.device
        saved_variables = th.load(str(path) + '_non_sb3_data', map_location=device)
        set_args_from_load(saved_variables['args'], args)
        worker = saved_variables['worker_type'].load(saved_variables['worker_path'], args)
        manager = saved_variables['manager_type'].load(saved_variables['manager_path'], args)
        saved_variables['const_params']['args'] = args

        # Create agent object
        model = cls(manager=manager, worker=worker, args=args)  # pytype: disable=not-instantiable
        model.to(device)
        return model

class ValueBasedManager(Manager):
    """
    Follows a few basic rules. (tm = teammate)
    1. All independent tasks values:
       a) Get onion from dispenser = (3 * num_pots) - 0.5 * num_onions
       b) Get plate from dish rack = num_filled_pots * (2
       c)
       d)
    2. Supporting tasks
       a) Start at a value of zero
       b) Always increases in value by a small amount (supporting is good)
       c) If one is performed and the tm completes the complementary task, then the task value is increased
       d) If the tm doesn't complete the complementary task, after a grace period the task value starts decreasing
          until the object is picked up
    3. Complementary tasks:
       a) Start at a value of zero
       b) If a tm performs a supporting task, then its complementary task value increases while the object remains
          on the counter.
       c) If the object is removed from the counter, the complementary task value is reset to zero (the
          complementary task cannot be completed if there is no object to pick up)
    :return:
    """
    def __init__(self, worker, p_idx, args):
        super(ValueBasedManager, self).__init__(worker,'value_based_subtask_adaptor', p_idx, args)
        self.worker = worker
        assert worker.p_idx == p_idx
        self.trajectory = []
        self.terrain = OvercookedGridworld.from_layout_name(args.layout_name).terrain_mtx
        # for i in range(len(self.terrain)):
        #     self.terrain[i] = ''.join(self.terrain[i])
        # self.terrain = str(self.terrain)
        self.worker_subtask_counts = np.zeros((2, Subtasks.NUM_SUBTASKS))
        self.subtask_selection = args.subtask_selection


        self.init_subtask_values()

    def init_subtask_values(self):
        self.subtask_values = np.zeros(Subtasks.NUM_SUBTASKS)
        # 'unknown' subtask is always set to 0 since it is more a relic of labelling than a useful subtask
        # Independent subtasks
        self.ind_subtask = ['get_onion_from_dispenser', 'put_onion_in_pot', 'get_plate_from_dish_rack', 'get_soup', 'serve_soup']
        # Supportive subtasks
        self.sup_subtask = ['put_onion_closer', 'put_plate_closer', 'put_soup_closer']
        self.sup_obj_to_subtask = {'onion': 'put_onion_closer', 'dish': 'put_plate_closer', 'soup': 'put_soup_closer'}
        # Complementary subtasks
        self.com_subtask = ['get_onion_from_counter', 'get_plate_from_counter', 'get_soup_from_counter']
        self.com_obj_to_subtask = {'onion': 'get_onion_from_counter', 'dish': 'get_plate_from_counter', 'soup': 'get_soup_from_counter'}
        for i_s in self.ind_subtask:
            # 1.a
            self.subtask_values[Subtasks.SUBTASKS_TO_IDS[i_s]] = 1
        for s_s in self.sup_subtask:
            # 2.a
            self.subtask_values[Subtasks.SUBTASKS_TO_IDS[s_s]] = 0
        for c_s in self.com_subtask:
            # 3.a
            self.subtask_values[Subtasks.SUBTASKS_TO_IDS[c_s]] = 0

        self.acceptable_wait_time = 10  # 2d
        self.sup_base_inc = 0.05  # 2b
        self.sup_success_inc = 1  # 2c
        self.sup_waiting_dec = 0.1  # 2d
        self.com_waiting_inc = 0.2  # 3d
        self.successful_support_task_reward = 1
        self.agent_objects = {}
        self.teammate_objects = {}

    def update_subtask_values(self, prev_state, curr_state):
        prev_objects = prev_state.objects.values()
        curr_objects = curr_state.objects.values()
        # TODO objects are only tracked by name and position, so checking equality fails because picking something up changes the objects position
        # 2.b
        for s_s in self.sup_subtask:
            self.subtask_values[Subtasks.SUBTASKS_TO_IDS[s_s]] += self.sup_base_inc

        # Analyze objects that are on counters
        for object in curr_objects:
            x, y = object.position
            if object.name == 'soup' and self.terrain[y][x] == 'P':
                # Soups while in pots can change without agent intervention
                continue
            # Objects that have been put down this turn
            if object not in prev_objects:
                if is_held_obj(prev_state.players[self.p_idx], object):
                    print(f'Agent placed {object}')
                    self.agent_objects[object] = 0
                elif is_held_obj(prev_state.players[self.t_idx], object):
                    print(f'Teammate placed {object}')
                    self.teammate_objects[object] = 0
                # else:
                #     raise ValueError(f'Object {object} has been put down, but did not belong to either player')
            # Objects that have not moved since the previous time step
            else:
                if object in self.agent_objects:
                    self.agent_objects[object] += 1
                    if self.agent_objects[object] > self.acceptable_wait_time:
                        # 2.d
                        subtask_id = Subtasks.SUBTASKS_TO_IDS[self.sup_obj_to_subtask[object.name]]
                        self.subtask_values[subtask_id] -= self.sup_waiting_dec
                elif object in self.teammate_objects:
                    # 3.b
                    self.teammate_objects[object] += 1
                    subtask_id = Subtasks.SUBTASKS_TO_IDS[self.com_obj_to_subtask[object.name]]
                    self.subtask_values[subtask_id] += self.com_waiting_inc

        for object in prev_objects:
            x, y = object.position
            if object.name == 'soup' and self.terrain[y][x] == 'P':
                # Soups while in pots can change without agent intervention
                continue
            # Objects that have been picked up this turn
            if object not in curr_objects:
                if is_held_obj(curr_state.players[self.p_idx], object):
                    print(f'Agent picked up {object}')
                    if object in self.agent_objects:
                        del self.agent_objects[object]
                    else:
                        del self.teammate_objects[object]

                elif is_held_obj(curr_state.players[self.t_idx], object):
                    print(f'Teammate picked up {object}')
                    if object in self.agent_objects:
                        # 2.c
                        subtask_id = Subtasks.SUBTASKS_TO_IDS[self.sup_obj_to_subtask[object.name]]
                        self.subtask_values[subtask_id] += self.sup_success_inc
                        del self.agent_objects[object]
                    else:
                        del self.teammate_objects[object]
                # else:
                #     raise ValueError(f'Object {object} has been picked up, but does not belong to either player')

                # Find out if there are any remaining objects of the same type left
                last_object_of_this_type = True
                for rem_objects in list(self.agent_objects) + list(self.teammate_objects):
                    if object.name == rem_objects.name:
                        last_object_of_this_type = False
                        break
                # 3.c
                if last_object_of_this_type:
                    subtask_id = Subtasks.SUBTASKS_TO_IDS[self.com_obj_to_subtask[object.name]]
                    self.subtask_values[subtask_id] = 0

        self.subtask_values = np.clip(self.subtask_values, 0, 10)

    def get_subtask_values(self, curr_state):
        assert self.subtask_values is not None
        return self.subtask_values * self.doable_subtasks(curr_state, self.terrain, self.p_idx)

    def select_next_subtask(self, curr_state):
        subtask_values = self.get_subtask_values(curr_state)
        subtask_id = np.argmax(subtask_values.squeeze(), dim=-1)
        self.curr_subtask_id = subtask_id
        print('new subtask', Subtasks.IDS_TO_SUBTASKS[subtask_id.item()])

    def reset(self, state):
        super().reset(state)
        self.init_subtask_values()

class DistBasedManager(Manager):
    def __init__(self, agent, p_idx, args):
        super(DistBasedManager, self).__init__(agent, p_idx, args)
        self.name = 'dist_based_subtask_agent'

    def distribution_matching(self, subtask_logits, egocentric=False):
        """
        Try to match some precalculated 'optimal' distribution of subtasks.
        If egocentric look only at the individual player distribution, else look at the distribution across both players
        """
        assert self.optimal_distribution is not None
        if egocentric:
            curr_dist = self.worker_subtask_counts[self.p_idx]
            best_dist = self.optimal_distribution[self.p_idx]
        else:
            curr_dist = self.worker_subtask_counts.sum(axis=0)
            best_dist = self.optimal_distribution.sum(axis=0)
        curr_dist = curr_dist / np.sum(curr_dist)
        dist_diff = best_dist - curr_dist

        pred_subtask_probs = F.softmax(subtask_logits).detach().numpy()
        # TODO investigate weighting
        # TODO should i do the above softmax?
        # Loosely based on Bayesian inference where prior is the difference in distributions, and the evidence is
        # predicted probability of what subtask should be done
        adapted_probs = pred_subtask_probs * dist_diff
        adapted_probs = adapted_probs / np.sum(adapted_probs)
        return adapted_probs

    def select_next_subtask(self, curr_state):
        # TODO
        pass

    def reset(self, state, player_idx):
        # TODO
        pass

if __name__ == '__main__':
    args = get_arguments()
    p_idx, t_idx = 0, 1
    # worker, teammate = MultiAgentSubtaskWorker.create_model_from_scratch(p_idx, args, dataset_file=args.dataset)

    worker = MultiAgentSubtaskWorker.load(
        '/projects/star7023/oai/agent_models/multi_agent_subtask_worker/counter_circuit_o_1order/fr', args)

    bct = BehavioralCloningTrainer(args.dataset, args)
    bct.train_agents(epochs=50)
    teammate = bct.get_agents(p_idx=t_idx)



    #create_rl_worker(args.dataset, p_idx, args)
    # tsat = MultipleAgentsTrainer(args)
    # tsat.train_agents(total_timesteps=1e6)
    # teammate = tsat.get_agent(t_idx)
    rlmt = RLManagerTrainer(worker, [teammate], t_idx, args)
    rlmt.train_agents(total_timesteps=1e7, exp_name=args.exp_name + '_manager')
    print('done')


