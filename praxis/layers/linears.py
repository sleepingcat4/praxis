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

"""Linear layers."""

from typing import Optional

from jax import numpy as jnp
from jax import vmap
from praxis import base_layer
from praxis import py_utils
from praxis import pytypes
from praxis.layers import activations

NestedMap = py_utils.NestedMap
WeightInit = base_layer.WeightInit
WeightHParams = base_layer.WeightHParams
sub_config_field = base_layer.sub_config_field

BaseHParams = base_layer.BaseLayer.HParams
JTensor = pytypes.JTensor


def project_last_dim(inputs: JTensor, weight: JTensor) -> JTensor:
  """Linear projection on the last dim of the input JTensor.

  This is a TPU efficient implementation to avoid reshaping inputs to Rank-2
  JTensor by using Einsum for the compute.

  Args:
    inputs: An input JTensor, the last dimension of which is input_dim.
    weight: A weight matrix with shape [input_dim, output_dim].

  Returns:
    An output JTensor of the same rank as inputs, the last dimension is
    output_dim.
  """
  input_shape = inputs.shape
  assert len(input_shape) >= 2
  weight_shape = weight.shape
  assert len(weight_shape) == 2
  assert input_shape[-1] == weight_shape[0], (
      f'input_shape[-1] = {input_shape[-1]}, '
      f'weight_shape[0] = {weight_shape[0]}')
  return jnp.einsum('...y,yz->...z', inputs, weight)


class Linear(base_layer.BaseLayer):
  """Linear layer without bias."""

  class HParams(BaseHParams):
    """Associated hyperparams for this layer class.

    Attributes:
      input_dims: Depth of the input.
      output_dims: Depth of the output.
    """
    input_dims: int = 0
    output_dims: int = 0

  def setup(self) -> None:
    p = self.hparams
    wp = p.weight_split_dims_mapping
    self.create_variable(
        'w',
        WeightHParams(
            shape=[p.input_dims, p.output_dims],
            mesh_shape=p.mesh_shape,
            tensor_split_dims_mapping=wp.wt))

  def fprop(self, inputs: JTensor) -> JTensor:
    """Apply projection to inputs.

    Args:
      inputs: The inputs JTensor.  Shaped [..., input_dims].

    Returns:
      Projected inputs.
    """
    p = self.hparams
    ap = p.activation_split_dims_mapping
    out = project_last_dim(inputs, self.theta.w)
    # Adjust sharding annotation during decoding.
    if ap.out is not None and len(ap.out) == 3 and out.ndim == 2:
      ap.out = [ap.out[0], ap.out[2]]
    out = base_layer.maybe_shard(out, ap.out, p.mesh_axis_names)
    return out


class Bias(base_layer.BaseLayer):
  """Bias layer."""

  class HParams(BaseHParams):
    """Associated hyperparams for this layer class.

    Attributes:
      dims: Depth of the input.
      bias_init: Init scale (constant) of bias terms.
    """
    dims: int = 0
    bias_init: Optional[float] = 0.0

  def setup(self) -> None:
    p = self.hparams
    wp = p.weight_split_dims_mapping
    self.create_variable(
        'b',
        WeightHParams(
            shape=[p.dims],
            init=WeightInit.Constant(p.bias_init),
            mesh_shape=p.mesh_shape,
            tensor_split_dims_mapping=wp.wt))

  def fprop(self, inputs: JTensor) -> JTensor:
    """Adds bias to inputs.

    Args:
      inputs: The inputs JTensor.  Shaped [..., dims].

    Returns:
      Inputs plus bias.
    """
    return inputs + self.theta.b


class FeedForward(base_layer.BaseLayer):
  """Feedforward layer with activation."""

  class HParams(BaseHParams):
    """Associated hyperparams for this layer class.

    Attributes:
      input_dims: Depth of the input.
      output_dims: Depth of the output.
      has_bias: Adds bias weights or not.
      linear_tpl: Linear layer params
      activation: Activation function to use. Options are RELU, RELU6, RELU^2,
        RELU^3, LEAKY_RELU, SIGMOID, TANH, GELU, NONE.
      negative_slope: Negative slope of LEAKY_RELU activation.
      bias_init: Init scale (constant) of bias terms.
    """
    input_dims: int = 0
    output_dims: int = 0
    has_bias: bool = True
    linear_tpl: BaseHParams = sub_config_field(Linear.HParams)
    activation: str = 'RELU'
    negative_slope: Optional[float] = None
    bias_init: Optional[float] = 0.0

  def setup(self) -> None:
    p = self.hparams
    wp = p.weight_split_dims_mapping
    ap = p.activation_split_dims_mapping
    linear_layer_p = p.linear_tpl.clone()
    linear_layer_p.set(
        input_dims=p.input_dims,
        output_dims=p.output_dims,
        weight_split_dims_mapping=wp.clone(),
        activation_split_dims_mapping=ap.clone())
    # Provide type hint.
    self.linear: Linear
    self.create_child('linear', linear_layer_p)
    if p.has_bias:
      bias_layer_p = Bias.HParams(dims=p.output_dims, bias_init=p.bias_init)
      if p.mesh_shape is not None and ap.out is not None:
        wp_bias = [ap.out[-1]]
        bias_layer_p.weight_split_dims_mapping.wt = wp_bias
      # Provide type hint.
      self.bias: Bias
      self.create_child('bias', bias_layer_p)
    act_p = activations.Activation.HParams(
        activation=p.activation, negative_slope=p.negative_slope)
    # Provide type hints
    self.activation: activations.Activation
    self.create_child('activation', act_p)

  def fprop(self, inputs: JTensor) -> JTensor:
    projected_inputs = self.linear.fprop(inputs)
    if self.hparams.has_bias:
      projected_inputs = self.bias.fprop(projected_inputs)
    output = self.activation.fprop(projected_inputs)
    return output


class MLPBlock(base_layer.BaseLayer):
  """Feedforward layer with activation."""

  class HParams(BaseHParams):
    """Associated hyperparams for this layer class.

    Attributes:
      num_layers: Number of FeedForward layers.
      hidden_dims: Dimension of hidden layers.
      ff_tpl: Feedforward layer params.
    """
    num_layers: int = 3
    hidden_dims: int = 128
    ff_tpl: BaseHParams = sub_config_field(FeedForward.HParams)

  def setup(self) -> None:
    p = self.hparams

    wp = p.weight_split_dims_mapping
    ap = p.activation_split_dims_mapping
    input_layer_p = p.ff_tpl.clone()
    input_layer_p.set(
        input_dims=p.ff_tpl.input_dims,
        output_dims=p.hidden_dims,
        weight_split_dims_mapping=wp.clone(),
        activation_split_dims_mapping=ap.clone())
    hidden_layer_p = p.ff_tpl.clone()
    hidden_layer_p.set(
        input_dims=p.hidden_dims,
        output_dims=p.hidden_dims,
        weight_split_dims_mapping=wp.clone(),
        activation_split_dims_mapping=ap.clone())
    output_layer_p = p.ff_tpl.clone()
    output_layer_p.set(
        input_dims=p.hidden_dims,
        output_dims=p.ff_tpl.output_dims,
        weight_split_dims_mapping=wp.clone(),
        activation_split_dims_mapping=ap.clone())
    mlp_layers = [input_layer_p]
    for _ in range(p.num_layers - 2):
      mlp_layers.append(hidden_layer_p)
    mlp_layers.append(output_layer_p)
    self.create_children('mlp_layers', mlp_layers)

  def fprop(self, inputs: JTensor) -> JTensor:
    output = inputs
    p = self.hparams
    for i in range(p.num_layers):
      output = self.mlp_layers[i].fprop(output)
    return output


class StackingOverTime(base_layer.BaseLayer):
  """Stacking applied along the time axis.

     At each time step of an input sequence, elements are stacked over the
     window of ('left_context' + 1 + 'right_context') steps around the current
     time step. Zeros will be padded to the left or right of the sequence for
     elements around the boundaries. Finally the stacked outputs are emitted
     once every 'stride' steps.

     E.g. if an input sequence is: [4], [1], [9], [3], [5], [2], [8]
     left_context = 1, right_context = 1, stride = 3,
     then the output sequence would be: [0, 4, 1], [9, 3, 5], [2, 8, 0]

     Note that this layer only performs tensor transformation, so there are no
     learnable parameters.
  """

  class HParams(BaseHParams):
    """Associated hyperparams for this layer class.

    Attributes:
      left_context: Number of time steps to stack on the left to the central
        step.
      right_context: Number of time steps to stack on the right to the central
        step.
      stride: The stride for emitting the stacked output.
      pad_with_left_frame: Whether to use the left frame for padding instead of
        0s.
      pad_with_right_frame: Whether to use the right frame for padding instead
        of 0s.
      padding_reduce_option: reduce_max or reduce_min. How to reduce stacked
        padding from [b, t / stride, stride] to [b, t / stride, 1].
    """
    left_context: int = 0
    right_context: int = 0
    stride: int = 0
    pad_with_left_frame: bool = False
    pad_with_right_frame: bool = False
    padding_reduce_option: str = 'reduce_min'

  def setup(self) -> None:
    p = self.hparams
    assert p.name
    assert p.left_context >= 0, p.left_context
    assert p.right_context >= 0, p.right_context
    assert p.stride >= 1
    assert p.padding_reduce_option in ('reduce_min', 'reduce_max')

  @property
  def window_size(self):
    """Returns the stacking window size.

    The output dimension will be window_size * the input dimension.

    Returns:
      Window size.
    """
    p = self.hparams
    return p.left_context + p.right_context + 1

  def _applystack(self, inputs, pad_value=0.0):
    """The core function to apply the stacking to inputs.

    Args:
      inputs: [batch, time, depth].
      pad_value: the padding value for left/right context.

    Returns:
      [batch, ceil(time / stride), depth * stacking_window_length] tensor.
    """
    p = self.hparams
    if p.left_context == 0 and p.right_context == 0:
      out = inputs
    else:
      inputs_max_len = inputs.shape[1]
      left_to_pad = p.left_context
      right_to_pad = p.right_context
      # optionally copy left frame N times
      if p.pad_with_left_frame:
        left_pad = jnp.repeat(inputs[:, :1, :], repeats=p.left_context, axis=1)
        inputs = jnp.concatenate([left_pad, inputs], axis=1)
        left_to_pad = 0

      # optionally copy right frame N times
      if p.pad_with_right_frame:
        right_pad = jnp.repeat(
            inputs[:, -1:, :], repeats=p.right_context, axis=1)
        inputs = jnp.concatenate([inputs, right_pad], axis=1)
        right_to_pad = 0

      # Add zero paddings to the left and right of the input sequence.
      inputs = jnp.pad(
          inputs, [[0, 0], [left_to_pad, right_to_pad], [0, 0]],
          constant_values=pad_value)

      # Make window_size() copies of the padded sequence with the original
      # sequence length, where each copy is offset by 1 time step.
      pieces = []
      for i in range(self.window_size):
        pieces.append(inputs[:, i:i + inputs_max_len])
      # Apply stacking.
      out = jnp.concatenate(pieces, 2)

    # Apply striding.
    out = out[:, ::p.stride]
    return out

  def fprop(self, inputs, paddings=None):
    """Apply the stacking to inputs along the time axis.

    Args:
      inputs: The inputs tensor. It is expected to be of shape [batch, time,
        feature].
      paddings: The paddings tensor. It is expected to be of shape [batch, time,
        1], where all but the last dimension match inputs. Each value is 0 or 1
        indicating whether a time step of a sequence is padded in the inputs to
        reach the max length in the batch.

    Returns:
      (outputs, out_paddings) pair.
        outputs is of shape [batch, ceil(time / stride), feature * stacking].
        out_paddings is of shape [batch, ceil(time / stride), 1]. out_paddings
        will be 0 if any of the corresponding input padding is 0.
    """
    p = self.hparams
    if paddings is None:
      paddings = jnp.zeros(
          jnp.concatenate([jnp.array(inputs.shape[:-1]),
                           jnp.array([1])]),
          dtype=inputs.dtype)

    # Checks the inputs shape, paddings has 3 dimensions.
    base_layer.assert_has_shape(inputs, [-1, -1, -1])
    base_layer.assert_has_shape(paddings, [-1, -1, 1])

    # Trivial case.
    if 0 == p.left_context == p.right_context and 1 == p.stride:
      return inputs, paddings

    outputs = self._applystack(inputs)

    # Stack the padding values with the same context and stride parameters.
    # Then take the minimum padding values within each stacking window, since
    # an output time step becomes a padded one only if all of the underlying
    # stacked steps are padded ones.
    out_paddings = self._applystack(paddings, pad_value=1)
    if p.padding_reduce_option == 'reduce_min':
      out_paddings = jnp.amin(out_paddings, axis=2, keepdims=True)
    else:
      out_paddings = jnp.amax(out_paddings, axis=2, keepdims=True)

    return outputs, out_paddings

  def unstack(self, stacked):
    """Inverts stacking over time.

    Given 'stacked' outputs from this StackingOverTime layer,

      stacked, _ = this_layer.FProp(inputs),

    this method attempts to reconstruct the original 'inputs'.

    If stride > window_size, the original input cannot be recovered, and a
    ValueError is raised.

    Otherwise, if right_context + 1 >= stride, this method returns a Tensor that
    is identical to 'inputs' but potentially longer due to paddings.

    If right_context + 1 < stride, this method returns a Tensor that may be up
    to ```stride - right_context - 1``` frames shorter than the original input,
    but identical in the frames that are returned. e.g.::

      left_context = 2, right_context = 1, stride = 4
      input sequence:     1 2 3 4 5 6 7 8
      after padding:  0 0 1 2 3 4 5 6 7 8 0
      windows:
        [0 0 (1) 2] 3 4 5 6 7 8 0
         0 0 1 2 [3 4 (5) 6] 7 8 0
      stacked:
        [[0 0 1 2], [3 4 5 6]]
      unstacked:
        [1 2 3 4 5 6], which is 4 - 1 - 1 = 2 (stride - right_context - 1)
        frames shorter than the original input.

    `unstack()` can be used to project the outputs of downstream layers back to
    the shape of the original unstacked inputs. For example::

        inputs = ...  # [batch, length, input_dim]
        # [batch, ceil(length / stride), rnn_dim]
        rnn_out = rnn.fprop(stacking.fprop(inputs)[0])
        # [batch, length, rnn_dim]
        back_projected_rnn_out = py_utils.PadOrTrimTo(
            stacking.unstack(jnp.tile(rnn_out, [1, 1, stacking.window_size])),
            inputs.shape)

    Note this method does not take or return a separate padding JTensor. The
    caller is responsible for knowing which of outputs are padding (e.g. based
    on the padding of the original FProp inputs).

    Args:
      stacked: JTensor of shape [batch, time, window_size * feature_dim],
        assumed to be the output of `fprop`.

    Returns:
      The reconstructed input JTensor, with shape
      [batch, (frames - 1) * stride + right_context + 1, feature_dim].

    Raises:
      ValueError: if stride > window_size.
    """
    p = self.hparams
    if 0 == p.left_context == p.right_context and 1 == p.stride:
      return stacked

    if p.stride > self.window_size:
      raise ValueError(
          "Can't invert StackingOverTime with stride (%d) > window_size (%d)" %
          (p.stride, self.window_size))

    # Reshape to allow indexing individual frames within each stacked window.
    batch_size, stacked_length, _ = stacked.shape
    stacked = jnp.reshape(stacked,
                          [batch_size, stacked_length, self.window_size, -1])

    # Compute the index of the window and frame in 'stacked' where each frame of
    # the original input is located, and extract them with tf.gather_nd.
    # First compute for all except the last window, since these elements have
    # the potential of being looked up from the next window.
    input_indices = jnp.arange(0, (stacked_length - 1) * p.stride)
    mod = input_indices % p.stride
    in_next_window = jnp.greater(mod, p.right_context).astype(jnp.int32)
    window_index = input_indices // p.stride + in_next_window
    frame_index = p.left_context + mod - p.stride * in_next_window
    # Now handle the last window explicitly and concatenate onto the existing
    # window_index/frame_index tensors.
    last_window_length = p.right_context + 1
    window_index = jnp.concatenate([
        window_index,
        jnp.repeat(jnp.array([stacked_length - 1]), last_window_length)
    ],
                                   axis=0)
    frame_index = jnp.concatenate(
        [frame_index, p.left_context + jnp.arange(last_window_length)], axis=0)
    # Stack the indices for gather_nd operation below
    window_and_frame_indices = jnp.stack([window_index, frame_index], axis=1)
    window_and_frame_indices = jnp.tile(
        jnp.expand_dims(window_and_frame_indices, 0), [batch_size, 1, 1])

    # jax equivalent of tf.gather_nd
    def gather_nd_unbatched(params, indices):
      return params[tuple(jnp.moveaxis(indices, -1, 0))]

    return vmap(gather_nd_unbatched, (0, 0), 0)(stacked,
                                                window_and_frame_indices)
