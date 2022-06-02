# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""All functions related to loss computation and optimization.
"""

import torch
import torch.optim as optim
import numpy as np
from models import utils as mutils
from sde_lib import VESDE, VPSDE


def get_optimizer(config, params):
  """Returns a flax optimizer object based on `config`."""
  if config.optim.optimizer == 'Adam':
    optimizer = optim.Adam(params, lr=config.optim.lr, betas=(config.optim.beta1, 0.999), eps=config.optim.eps,
                           weight_decay=config.optim.weight_decay)
  else:
    raise NotImplementedError(
      f'Optimizer {config.optim.optimizer} not supported yet!')

  return optimizer


def optimization_manager(config):
  """Returns an optimize_fn based on `config`."""

  def optimize_fn(optimizer, params, step, lr=config.optim.lr,
                  warmup=config.optim.warmup,
                  grad_clip=config.optim.grad_clip):
    """Optimizes with warmup and gradient clipping (disabled if negative)."""
    if warmup > 0:
      for g in optimizer.param_groups:
        g['lr'] = lr * np.minimum(step / warmup, 1.0)
    if grad_clip >= 0:
      torch.nn.utils.clip_grad_norm_(params, max_norm=grad_clip)
    optimizer.step()

  return optimize_fn


def get_sde_loss_fn(sde, train, reduce_mean=True, continuous=True, likelihood_weighting=True, eps=1e-5):
  """Create a loss function for training with arbirary SDEs.

  Args:
    sde: An `sde_lib.SDE` object that represents the forward SDE.
    train: `True` for training loss and `False` for evaluation loss.
    reduce_mean: If `True`, average the loss across data dimensions. Otherwise sum the loss across data dimensions.
    continuous: `True` indicates that the model is defined to take continuous time steps. Otherwise it requires
      ad-hoc interpolation to take continuous time steps.
    likelihood_weighting: If `True`, weight the mixture of score matching losses
      according to https://arxiv.org/abs/2101.09258; otherwise use the weighting recommended in our paper.
    eps: A `float` number. The smallest time step to sample from.

  Returns:
    A loss function.
  """
  reduce_op = torch.mean if reduce_mean else lambda *args, **kwargs: 0.5 * torch.sum(*args, **kwargs)

  def loss_fn(model, batch):
    """Compute the loss function.

    Args:
      model: A score model.
      batch: A mini-batch of training data.

    Returns:
      loss: A scalar that represents the average loss value across the mini-batch.
    """

    batch.requires_grad = True

    score_fn = mutils.get_score_fn(sde, model, train=train, continuous=continuous)
    t = torch.rand(batch.shape[0], device=batch.device) * (sde.T - eps) + eps
    z = torch.randn_like(batch)
    mean, std = sde.marginal_prob(batch, t)
    perturbed_data = mean + std[:, None, None, None] * z
    score = score_fn(perturbed_data, t)

    # -----------------------------------------------------
    #           Compute Poincare loss (H loss): 
    # -----------------------------------------------------

    h_lambda = 1.
    epsilon_dist = 'Gaussian'  # Choose from ['Gaussian', 'Rademacher']
    n_samples = 100
    H_loss = H_loss_estimate(score, batch, epsilon_dist, n_samples)

    batch.requires_grad = False
    
    # -----------------------------------------------------
    #            Compute Score-matching loss: 
    # -----------------------------------------------------

    if not likelihood_weighting:
      losses = torch.square(score * std[:, None, None, None] + z)
      losses = reduce_op(losses.reshape(losses.shape[0], -1), dim=-1)
    else:
      g2 = sde.sde(torch.zeros_like(batch), t)[1] ** 2
      losses = torch.square(score + z / std[:, None, None, None])
      losses = reduce_op(losses.reshape(losses.shape[0], -1), dim=-1) * g2

    loss = torch.mean(losses)

    return loss + (h_lambda * H_loss), loss, H_loss

  return loss_fn


def H_loss_estimate(score, batch, epsilon_dist, n_samples):
  '''
    n_samples: number of epsilon samples
    batch: tesor of the data
    score: the model's output
  '''

  ## Need to average over a sufficient number of runs to attain an arbitrarily small error.
  h_losses_estimate_all = torch.zeros(batch.size(0), device=batch.device)
  for i in range(n_samples):

    # --- Sample epsilon1 and epsilon2 for each data point ans each feature:
    if epsilon_dist == 'Gaussian':
      eps1 = torch.randn_like(score)  # (32, 1, 32, 32)
      eps2 = torch.randn_like(score)  # (32, 1, 32, 32)
    elif epsilon_dist == 'Rademacher':
      eps1 = torch.randint_like(score, low=0, high=2).float() * 2 - 1.  # (32, 1, 32, 32)
      eps2 = torch.randint_like(score, low=0, high=2).float() * 2 - 1.  # (32, 1, 32, 32)

    # --- Compute A part (short):
    # dot product of (eps2 * score) per data point:
    eps2_prod_score = torch.sum(eps2 * score)  # scalar
    # grad of eps2_prod_score w.r.t the batch, per data point:
    grad_eps2_prod_score = torch.autograd.grad(outputs=eps2_prod_score, inputs=batch, retain_graph=True)[0]  # (32, 1, 32, 32)
    # dot product of (eps1 * grad_eps2_prod_score) per data point:
    A = torch.sum(eps1 * grad_eps2_prod_score, dim=tuple(range(1, len(grad_eps2_prod_score.shape))))  # (bs,)

    # --- Compute B part (short):
    # dot product of (eps1 * score) per data point:
    eps1_prod_score = torch.sum(eps1 * score)  # scalar
    # grad of eps1_prod_score w.r.t the batch, per data point:
    grad_eps1_prod_score = torch.autograd.grad(outputs=eps1_prod_score, inputs=batch, retain_graph=True)[0]  # (32, 1, 32, 32)
    # dot product of (eps2 * grad_eps1_prod_score) per data point:
    B = torch.sum(eps2 * grad_eps1_prod_score, dim=tuple(range(1, len(grad_eps1_prod_score.shape))))  # (bs,)

    # --- Compute the loss:
    h_losses_estimate = torch.pow(A - B, 2)  # (bs,)
    h_losses_estimate_all = h_losses_estimate_all + h_losses_estimate

  h_losses_estimate_all = h_losses_estimate_all / n_samples

  h_loss_estimate = torch.mean(h_losses_estimate_all)  # scalar  # torch.mean(H_losses)  # scalar
  
  return h_loss_estimate


def get_smld_loss_fn(vesde, train, reduce_mean=False):
  """Legacy code to reproduce previous results on SMLD(NCSN). Not recommended for new work."""
  assert isinstance(vesde, VESDE), "SMLD training only works for VESDEs."

  # Previous SMLD models assume descending sigmas
  smld_sigma_array = torch.flip(vesde.discrete_sigmas, dims=(0,))
  reduce_op = torch.mean if reduce_mean else lambda *args, **kwargs: 0.5 * torch.sum(*args, **kwargs)

  def loss_fn(model, batch):
    model_fn = mutils.get_model_fn(model, train=train)
    labels = torch.randint(0, vesde.N, (batch.shape[0],), device=batch.device)
    sigmas = smld_sigma_array.to(batch.device)[labels]
    noise = torch.randn_like(batch) * sigmas[:, None, None, None]  # same as batch size, where in row i there is noise using some sigma that will be added to data point in row i.
    perturbed_data = noise + batch
    score = model_fn(perturbed_data, labels)  # What is the shape here?? why need labels?
    target = -noise / (sigmas ** 2)[:, None, None, None]
    losses = torch.square(score - target)
    losses = reduce_op(losses.reshape(losses.shape[0], -1), dim=-1) * sigmas ** 2
    loss = torch.mean(losses)
    return loss

  return loss_fn


def get_ddpm_loss_fn(vpsde, train, reduce_mean=True):
  """Legacy code to reproduce previous results on DDPM. Not recommended for new work."""
  assert isinstance(vpsde, VPSDE), "DDPM training only works for VPSDEs."

  reduce_op = torch.mean if reduce_mean else lambda *args, **kwargs: 0.5 * torch.sum(*args, **kwargs)

  def loss_fn(model, batch):
    model_fn = mutils.get_model_fn(model, train=train)
    labels = torch.randint(0, vpsde.N, (batch.shape[0],), device=batch.device)
    sqrt_alphas_cumprod = vpsde.sqrt_alphas_cumprod.to(batch.device)
    sqrt_1m_alphas_cumprod = vpsde.sqrt_1m_alphas_cumprod.to(batch.device)
    noise = torch.randn_like(batch)
    perturbed_data = sqrt_alphas_cumprod[labels, None, None, None] * batch + \
                     sqrt_1m_alphas_cumprod[labels, None, None, None] * noise
    score = model_fn(perturbed_data, labels)
    losses = torch.square(score - noise)
    losses = reduce_op(losses.reshape(losses.shape[0], -1), dim=-1)
    loss = torch.mean(losses)
    return loss

  return loss_fn


def get_step_fn(sde, train, optimize_fn=None, reduce_mean=False, continuous=True, likelihood_weighting=False):
  """Create a one-step training/evaluation function.

  Args:
    sde: An `sde_lib.SDE` object that represents the forward SDE.
    optimize_fn: An optimization function.
    reduce_mean: If `True`, average the loss across data dimensions. Otherwise sum the loss across data dimensions.
    continuous: `True` indicates that the model is defined to take continuous time steps.
    likelihood_weighting: If `True`, weight the mixture of score matching losses according to
      https://arxiv.org/abs/2101.09258; otherwise use the weighting recommended by our paper.

  Returns:
    A one-step function for training or evaluation.
  """
  if continuous:
    loss_fn = get_sde_loss_fn(sde, train, reduce_mean=reduce_mean,
                              continuous=True, likelihood_weighting=likelihood_weighting)
  else:
    assert not likelihood_weighting, "Likelihood weighting is not supported for original SMLD/DDPM training."
    if isinstance(sde, VESDE):
      loss_fn = get_smld_loss_fn(sde, train, reduce_mean=reduce_mean)
    elif isinstance(sde, VPSDE):
      loss_fn = get_ddpm_loss_fn(sde, train, reduce_mean=reduce_mean)
    else:
      raise ValueError(f"Discrete training for {sde.__class__.__name__} is not recommended.")

  def step_fn(state, batch):
    """Running one step of training or evaluation.

    This function will undergo `jax.lax.scan` so that multiple steps can be pmapped and jit-compiled together
    for faster execution.

    Args:
      state: A dictionary of training information, containing the score model, optimizer,
       EMA status, and number of optimization steps.
      batch: A mini-batch of training/evaluation data.

    Returns:
      loss: The average loss value of this state.
    """
    model = state['model']
    if train:
      optimizer = state['optimizer']
      optimizer.zero_grad()
      loss, los_sde, loss_h = loss_fn(model, batch)
      loss.backward()
      optimize_fn(optimizer, model.parameters(), step=state['step'])
      state['step'] += 1
      state['ema'].update(model.parameters())
    else:
      #with torch.no_grad():
      ema = state['ema']
      ema.store(model.parameters())
      ema.copy_to(model.parameters())
      loss, los_sde, loss_h = loss_fn(model, batch)
      ema.restore(model.parameters())

    return loss, los_sde, loss_h

  return step_fn
