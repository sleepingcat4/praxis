# coding=utf-8
# Copyright 2022 Google LLC.
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

"""Vanilla Beam search algorithm."""

from typing import Tuple

from flax import linen as nn
import jax
from jax import numpy as jnp
from praxis import base_layer
from praxis import decoder_hparams
from praxis import decoder_utils
from praxis import py_utils
from praxis import sample_decode

NestedMap = py_utils.NestedMap
JTensor = base_layer.JTensor
BeamSearchHParams = decoder_hparams.BeamSearchHParams
DecodeInfo = Tuple[JTensor, JTensor, JTensor, JTensor]


def update_topk_scores_with_eos(end_hyps: DecodeInfo,
                                cur_hyps: DecodeInfo) -> DecodeInfo:
  """Updates topk scores with eos.

  Args:
    end_hyps: Tuple (ids, decode_lengths, score, score_norms) with eos, each
      element has shape [beam_size, batch_size...].
    cur_hyps: Tuple (ids, score, decode_lengths, score_norms) without eos, each
      element has shape [beam_size, batch_size...].

  Returns:
    Updated end_hyps.
  """
  (end_ids, end_lengths, end_scores, end_scores_norm) = end_hyps
  (cur_ids, cur_lengths, cur_scores, cur_scores_norm) = cur_hyps
  beam_dim = 1
  k = end_ids.shape[beam_dim]
  m = cur_ids.shape[beam_dim]
  ids = jnp.concatenate([end_ids, cur_ids], beam_dim)
  lengths = jnp.concatenate([end_lengths, cur_lengths], beam_dim)
  scores = jnp.concatenate([end_scores, cur_scores], beam_dim)
  scores_norm = jnp.concatenate([end_scores_norm, cur_scores_norm], beam_dim)
  end_scores_norm, indices = jax.lax.top_k(scores_norm, k)
  one_hot = jax.nn.one_hot(indices, k + m, dtype=jnp.int32)
  end_ids = jnp.einsum("bkt,bjk->bjt", ids, one_hot)
  end_lengths = jnp.einsum("bk,bjk->bj", lengths, one_hot)
  end_scores = jnp.einsum("bk,bjk->bj", scores, one_hot)
  return (end_ids, end_lengths, end_scores, end_scores_norm)


def shuffle_state(x: JTensor, hyp_id: JTensor):
  """Shuffle cache state at beam dimension.

  Args:
    x: The decode state with shape [beam_size, ...].
    hyp_id: The desired beam ids with shape [beam_size, batch_size].

  Returns:
    A reshuffle of x on beam dimension.
  """
  # No need to shuffle R1 tensor such as time_step.
  if not isinstance(x, JTensor) or len(x.shape) < 2:
    return x
  return jax.vmap(
      lambda s, i: jnp.take(s, i, axis=0), in_axes=0, out_axes=0)(x, hyp_id)


def broadcast_beam_dim(x: JTensor, beam_dim: int, beam_size: int) -> JTensor:
  """Broadcasts the tensor beam_size times at beam dimension.

  Args:
    x: The input tensor of shape [batch, ...].
    beam_dim: Beam dimension.
    beam_size: Beam size in beam search.

  Returns:
    A tensor of shape [batch_size * beam_size, ...].
  """
  return jnp.repeat(
      jnp.expand_dims(x, beam_dim), repeats=beam_size, axis=beam_dim)


def beam_search(model: base_layer.BaseLayer,
                extend_step_fn: decoder_utils.ExtendStepFn,
                fprop_fn: decoder_utils.FPropFn,
                transform_state_fn: decoder_utils.TransformStateFn,
                target_prefix_ids: JTensor, target_prefix_paddings: JTensor,
                beam_search_hparams: BeamSearchHParams) -> NestedMap:
  """Vanilla beam search decode the input batch.

  Args:
    model: The model object.
    extend_step_fn: A function that takes in the decoded sequence at the current
      time step (with shape [B] or [B, P] where B corresponds to the batch size
      and P corresponds to a possible prefix) and returns `JTensor` corresponds
      to the logits of the next step.
    fprop_fn: A function that takes in the prefix information and initialize the
      decode cache states.
    transform_state_fn: A function that transforms the decode state.
    target_prefix_ids: The token ids that correspond to the target sequence,
      with shape [batch_size, prefix_sequence_length].
    target_prefix_paddings: The token paddings that correspond to the target
      sequence, with shape [batch_size, prefix_sequence_length].
    beam_search_hparams: Beam search hyper parameters.

  Returns:
    A NestedMap with `.decode_lengths` (vector of ints indicating the lengths
    of non-padding tokens in `.output_ids`, which includes the prefix)`,
    `.output_ids` (matrix of int ids with the
    decoded output), `.scores` (Scores of decoding sequence).
  """
  # TODO(b/229679837): Move right align prefix ids and paddings logic inside
  # the beam_search function.

  # max_decode_steps doesn't count the prefix part.
  assert beam_search_hparams.max_decode_steps is not None
  beam_dim = 1
  beam_size = beam_search_hparams.beam_size
  batch_size = target_prefix_ids.shape[0]
  max_prefix_len = target_prefix_ids.shape[1]
  eos_id = beam_search_hparams.eos_id
  seq_len = beam_search_hparams.max_decode_steps + max_prefix_len

  # Init decode state using fprop_fn, state seq size is max_prefix_len.
  fprop_fn(model, target_prefix_ids, target_prefix_paddings)

  # Pad max_decode_steps to the state.
  transform_state_fn(
      model, decoder_utils.pad_state_fn(beam_search_hparams.max_decode_steps))

  # Broadcast cache states before the while loop.
  def _broadcast_state_fn(x, batch_dim, time_dim):
    del time_dim
    return jnp.repeat(x, repeats=beam_size, axis=batch_dim)
  transform_state_fn(model, _broadcast_state_fn)

  # Set up init loop variables.
  val = NestedMap()
  loop_state_shape = (batch_size, beam_size)
  val.output_ids = jnp.zeros(
      shape=(batch_size, beam_size, seq_len), dtype=jnp.int32)
  val.end_scores = jnp.zeros(shape=loop_state_shape, dtype=jnp.float32)
  val.end_scores -= 1e9
  val.end_scores_norm = val.end_scores
  val.hyp_scores = jnp.zeros(shape=loop_state_shape, dtype=jnp.float32)
  # Penalize all hyps except the first
  val.hyp_scores -= jnp.arange(beam_size, dtype=jnp.float32) * 1e9

  # Gets prefix_lengths from target_prefix_paddings.
  prefix_lengths = jnp.sum(1 - target_prefix_paddings.astype(jnp.int32), axis=1)
  prefix_lengths = broadcast_beam_dim(
      prefix_lengths, beam_dim=beam_dim, beam_size=beam_size)
  # Update output_ids with prefix_ids.
  target_prefix_ids = broadcast_beam_dim(
      target_prefix_ids, beam_dim=beam_dim, beam_size=beam_size)
  val.output_ids = jax.lax.dynamic_update_slice(val.output_ids,
                                                target_prefix_ids,
                                                [0] * val.output_ids.ndim)
  val.end_ids = val.output_ids
  # Update loop init states with prefix.
  val.step = max_prefix_len - 1
  val.segment_pos = jnp.reshape(prefix_lengths - 1,
                                (batch_size * beam_size, -1))
  val.end_decode_lengths = jnp.ones_like(prefix_lengths) * seq_len

  def cond_func(model, val):
    """Whether the while loop should continue."""
    del model
    return val.step < seq_len - 1

  def loop_body(model, val):
    """From ids at `step`, update output ids at `step + 1`."""
    step = val.step
    logits = extend_step_fn(model, jnp.reshape(val.output_ids[:, :, step],
                                               (-1)), val.segment_pos)
    logits = jnp.reshape(logits, (batch_size, beam_size, -1))
    # TODO(b/229679837): consider add logprobs to while loop state and
    # shuffle it.
    logprobs = jax.nn.log_softmax(logits.astype(jnp.float32))
    # Select the best ids with eos.
    eos_scores = val.hyp_scores + logprobs[:, :, eos_id]
    new_end_ids = val.output_ids.at[:, :, step + 1].set(
        eos_id * jnp.ones_like(prefix_lengths))
    decode_length = step + 2
    new_decode_lengths = jnp.ones_like(prefix_lengths) * decode_length
    eos_scores_norm = eos_scores / decoder_utils.length_norm(
        decode_length - max_prefix_len, beam_search_hparams.length_norm_alpha)
    updated_topk_scores = update_topk_scores_with_eos(
        (val.end_ids, val.end_decode_lengths, val.end_scores,
         val.end_scores_norm),
        (new_end_ids, new_decode_lengths, val.hyp_scores, eos_scores_norm))
    (val.end_ids, val.end_decode_lengths, val.end_scores,
     val.end_scores_norm) = updated_topk_scores

    # Choose the topk indices.
    _, topk_indices, final_topk_value, final_topk_indices = (
        decoder_utils.two_stage_topk(logprobs, val.hyp_scores, eos_id))
    # update scores without EOS.
    val.hyp_scores = final_topk_value
    hyp_id = final_topk_indices // beam_size

    # Shuffle at beam dimension for the cache states using hyp_id.
    def _shuffle_state_fn(x, batch_dim, time_dim):
      del time_dim
      x_shape = x.shape
      new_shape = list(x_shape)
      new_shape.insert(batch_dim + 1, beam_size)
      new_shape[batch_dim] = x_shape[batch_dim] // beam_size
      new_state = shuffle_state(jnp.reshape(x, new_shape), hyp_id)
      return jnp.reshape(new_state, x_shape)

    transform_state_fn(model, _shuffle_state_fn)

    # Gather output ids
    new_ids = decoder_utils.gather_output_id(topk_indices, final_topk_indices)
    # TODO(b/229679837): add logic to stop early.

    # Shuffle output ids at beam dimension using hyp_id.
    val.output_ids = shuffle_state(val.output_ids, hyp_id)
    # Update output_ids.
    val.output_ids = val.output_ids.at[:, :, step + 1].set(new_ids)
    val.step += 1
    val.segment_pos += 1
    return val

  # Beam search loop. Cache state is broacasted before the while loop.
  result = nn.while_loop(
      cond_func,
      loop_body,
      model,
      val,
      carry_variables=[base_layer.DECODE_CACHE])

  result.output_ids = result.end_ids
  result.output_ids = sample_decode.left_align_output_sequence(
      jnp.reshape(result.output_ids, (batch_size * beam_size, -1)),
      jnp.reshape(prefix_lengths, (-1)), max_prefix_len)
  result.output_ids = jnp.reshape(result.output_ids,
                                  (batch_size, beam_size, -1))
  result.scores = result.end_scores_norm
  result.logprobs = result.scores
  result.decode_lengths = result.end_decode_lengths
  del (result.end_ids, result.end_scores, result.end_decode_lengths,
       result.end_scores_norm, result.hyp_scores)
  return result
