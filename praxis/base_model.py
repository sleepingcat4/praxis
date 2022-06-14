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

"""Base class for all models.

The model solely consists of the network, while the task combines one or several
models with one or several learners/optimizers.
"""

import dataclasses
from typing import Any, Dict, Optional, Sequence, Tuple, Union

from praxis import base_input
from praxis import base_layer
from praxis import py_utils
from praxis import pytypes

NestedMap = py_utils.NestedMap
JTensor = pytypes.JTensor
Metrics = pytypes.Metrics
Predictions = Union[JTensor, NestedMap, Dict[str, Any], Dict[int, Any]]
BaseHParams = base_layer.BaseLayer.HParams


class BaseModel(base_layer.BaseLayer):
  """An API that every model should be derived from."""

  def compute_predictions(self, input_batch: NestedMap) -> Predictions:
    """Computes predictions for `input_batch`.

    This method must be defined in a concrete derived class.

    The output can be in the form of probablistic distributions, e.g., softmax
    logits for discrete outputs, mixture of logistics for continuous values, or
    regression values.

    For training/evaluation, the output will be used for computing loss and
    gradient updates, including comparing predicted distributions between
    teacher and student for distillation. During inference the output can be
    used to compute final outputs, perhaps with sampling.

    Args:
      input_batch: A `.NestedMap` object containing input tensors.

    Returns:
      Predictions, either a single Tensor, a `.NestedMap`, or a namedtuple.
    """
    raise NotImplementedError('Abstract method')

  def compute_loss(self, predictions: Union[JTensor, NestedMap],
                   input_batch: NestedMap) -> Tuple[Metrics, Dict[str, Any]]:
    """Computes the loss and other metrics for the given predictions.

    This method must be defined in a concrete derived class.

    Args:
      predictions: The output of `compute_predictions`.
      input_batch: A `.NestedMap` object containing input tensors to this tower.

    Returns:
      - A dict or NestedMap containing str keys and (metric, weight) pairs as
        values, where one of the entries is expected to corresponds to the loss.
      - A dict containing arbitrary tensors describing something about each
        training example, where the first dimension of each tensor is the batch
        index.
    """
    raise NotImplementedError('Abstract method')

  def fprop(self, input_batch: NestedMap) -> Tuple[Metrics, Dict[str, Any]]:
    """Forward propagation through one tower of the model.

    Args:
      input_batch: A `.NestedMap` object containing input tensors to this tower.

    Returns:
      (dict, dict):

      - A dict containing str keys and (metric, weight) pairs as values, where
        one of the keys is expected to be 'loss'.
      - A dict containing arbitrary tensors describing something about each
        training example, where the first dimension of each tensor is the batch
        index.
    """
    predictions = self.compute_predictions(input_batch)
    return self.compute_loss(predictions, input_batch)

  def decode(self, input_batch: NestedMap) -> Tuple[NestedMap, NestedMap]:
    """Decodes input_batch.

    Args:
      input_batch: The input batch. A `NestedMap` of tensors. Or, if input batch
        spiltting is used, a list of `NestedMap`, one for each split.

    Returns:
      - metrics, a NestedMap containing str keys and (metric, weight) pairs for
        the current batch (a tuple of two scalars).
      - results, a `.NestedMap` as decoder output.
    """
    raise NotImplementedError('Abstract method')

  def process_decode_out(
      self, input_obj: base_input.BaseInput,
      decode_out: NestedMap) -> Tuple[NestedMap, Sequence[Tuple[str, Any]]]:
    """Processes one batch of decoded outputs.

    Args:
      input_obj: The input object where a tokenizer is accessible.
      decode_out: The output from decode(). May have an extra leading axis.

    Returns:
      - metrics, a NestedMap containing str keys and (metric, weight) pairs for
        the current batch (a tuple of two scalars).
      - A list of tuples where each element corresponds to a row in the batch.
        Each tuple is a key value pair.
    """
    raise NotImplementedError('Abstract method')


class LegosModel(BaseModel):
  """Legos - A set of components that can be co-trained or trained in parts."""

  class HParams(BaseModel.HParams):
    """Associated hyper-params for this layer class.

    Attributes:
      components: List of model components aggregated into a single legos model.
    """
    components: Optional[BaseHParams] = None

  def setup(self) -> None:
    """Build the mixer from the collection of components."""
    # TODO(b/227407216): Check that this is robust enough and/or fix if needed.
    for f in dataclasses.fields(self.hparams.components):
      if hasattr(self.hparams.components, f.name):
        self.create_child(f.name, getattr(self.hparams.components, f.name))

  def get_model_params(self, name: str) -> BaseModel.HParams:
    raise NotImplementedError('Abstract method')
