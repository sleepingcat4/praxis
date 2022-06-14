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

"""Data augmentation layers."""

from typing import Tuple

import jax
from jax import numpy as jnp
from praxis import base_layer
from praxis import py_utils
from praxis import pytypes

NestedMap = py_utils.NestedMap
JTensor = pytypes.JTensor

BaseHParams = base_layer.BaseLayer.HParams


class MaskedLmDataAugmenter(base_layer.BaseLayer):
  """Performs data augmentation according to the BERT paper.

  https://arxiv.org/pdf/1810.04805.pdf
  """

  class HParams(BaseHParams):
    """Associated hyper-params for this layer class.

    Attributes:
      vocab_size: The total vocabulary size.
      mask_prob: Probability at which a token is replaced by the special <MASK>
        token.
      random_prob: Probability at which a token is replaced by a random token.
      same_prob: Probability at which a token is replaced by itself.
      mask_token_id: Id of the special <MASK> token.
    """
    vocab_size: int = 0
    mask_prob: float = 0.12
    random_prob: float = 0.015
    same_prob: float = 0.015
    mask_token_id: int = -1

  def fprop(self, inputs: JTensor,
            paddings: JTensor) -> Tuple[JTensor, JTensor]:
    """Applies data augmentation by randomly masking/replacing tokens in inputs.

    Args:
      inputs: An int32 tensor of shape [batch, length].
      paddings: A 0/1 tensor of shape [batch, length].

    Returns:
      A pair <new_inputs, mask>:
      new_inputs: An int32 tensor of shape [batch, length]. The new token ids
        after data augmentation.
      mask: A 0/1 tensor. A "1" indicates the corresponding token at that
        position had undergone the data augmentation process.
    """
    p = self.hparams
    assert p.vocab_size > 0
    assert p.mask_token_id >= 0
    assert p.mask_prob + p.random_prob + p.same_prob < 1.0
    assert p.mask_prob + p.random_prob + p.same_prob > 0.0

    fprop_dtype = self.fprop_dtype

    def _uniform_sample(sample_p: float) -> JTensor:
      prng_key = self.next_prng_key()
      rnd_sample = jax.random.uniform(prng_key, inputs.shape)
      return (rnd_sample < sample_p).astype(fprop_dtype)

    total_replacement_prob = p.mask_prob + p.random_prob + p.same_prob
    # valid_tokens == 1.0 if the corresponding position is a valid token.
    valid_tokens = 1.0 - paddings.astype(fprop_dtype)
    # replacement == 1.0 if the corresponding token is to be replaced by
    # something else (mask, random, self).
    replacement_pos = valid_tokens * _uniform_sample(total_replacement_prob)
    no_replacement = 1.0 - replacement_pos

    # First sample the token positions to be masked out.
    remaining_prob = total_replacement_prob
    remaining_pos = replacement_pos
    mask_prob = p.mask_prob / remaining_prob
    # mask_pos == 1.0 if the corresponding token should be masked.
    mask_pos = remaining_pos * _uniform_sample(mask_prob)

    # Next sample the token positions to be replaced by random tokens.
    remaining_prob -= p.mask_prob
    remaining_pos -= mask_pos
    assert remaining_prob > 0.0
    random_prob = p.random_prob / remaining_prob
    random_pos = remaining_pos * _uniform_sample(random_prob)

    # Lastly, token positions to be replaced by self.
    self_pos = remaining_pos - random_pos

    random_tokens = jax.random.randint(self.next_prng_key(), inputs.shape, 0,
                                       p.vocab_size, inputs.dtype)
    mask_tokens = jnp.zeros_like(inputs) + p.mask_token_id

    input_dtype = inputs.dtype
    augmented = (
        inputs * no_replacement.astype(input_dtype) +
        mask_tokens * mask_pos.astype(input_dtype) +
        random_tokens * random_pos.astype(input_dtype) +
        inputs * self_pos.astype(input_dtype))

    return augmented, replacement_pos
