# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains a collection of models which operate on variable-length sequences.
"""
import math

import models
import video_level_models
import tensorflow as tf
import model_utils as utils

import tensorflow.contrib.slim as slim
from tensorflow import flags

FLAGS = flags.FLAGS
flags.DEFINE_integer("iterations", 30,
                     "Number of frames per batch for DBoF.")
flags.DEFINE_bool("dbof_add_batch_norm", True,
                  "Adds batch normalization to the DBoF model.")
flags.DEFINE_bool(
    "sample_random_frames", True,
    "If true samples random frames (for frame level models). If false, a random"
    "sequence of frames is sampled instead.")
flags.DEFINE_integer("dbof_cluster_size", 8192,
                     "Number of units in the DBoF cluster layer.")
flags.DEFINE_integer("dbof_hidden_size", 1024,
                     "Number of units in the DBoF hidden layer.")
flags.DEFINE_string("dbof_pooling_method", "max",
                    "The pooling method used in the DBoF cluster layer. "
                    "Choices are 'average' and 'max'.")
flags.DEFINE_string("video_level_classifier_model", "MoeModel",
                    "Some Frame-Level models can be decomposed into a "
                    "generalized pooling operation followed by a "
                    "classifier layer")
flags.DEFINE_integer("lstm_cells", 1024, "Number of LSTM cells.")
flags.DEFINE_integer("lstm_layers", 2, "Number of LSTM layers.")

class FrameLevelLogisticModel(models.BaseModel):

  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    """Creates a model which uses a logistic classifier over the average of the
    frame-level features.

    This class is intended to be an example for implementors of frame level
    models. If you want to train a model over averaged features it is more
    efficient to average them beforehand rather than on the fly.

    Args:
      model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                   input features.
      vocab_size: The number of classes in the dataset.
      num_frames: A vector of length 'batch' which indicates the number of
           frames for each video (before padding).

    Returns:
      A dictionary with a tensor containing the probability predictions of the
      model in the 'predictions' key. The dimensions of the tensor are
      'batch_size' x 'num_classes'.
    """
    num_frames = tf.cast(tf.expand_dims(num_frames, 1), tf.float32)
    feature_size = model_input.get_shape().as_list()[2]
    max_frames = model_input.get_shape().as_list()[1]


    denominators = tf.reshape(
        tf.tile(num_frames, [1, feature_size]), [-1, feature_size])
    avg_pooled = tf.reduce_sum(model_input,
                               axis=[1]) / denominators
    output = slim.fully_connected(
        avg_pooled, vocab_size, activation_fn=tf.nn.sigmoid,
        weights_regularizer=slim.l2_regularizer(1e-8))
    return {"predictions": output}

class DbofModel(models.BaseModel):
  """Creates a Deep Bag of Frames model.

  The model projects the features for each frame into a higher dimensional
  'clustering' space, pools across frames in that space, and then
  uses a configurable video-level model to classify the now aggregated features.

  The model will randomly sample either frames or sequences of frames during
  training to speed up convergence.

  Args:
    model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                 input features.
    vocab_size: The number of classes in the dataset.
    num_frames: A vector of length 'batch' which indicates the number of
         frames for each video (before padding).

  Returns:
    A dictionary with a tensor containing the probability predictions of the
    model in the 'predictions' key. The dimensions of the tensor are
    'batch_size' x 'num_classes'.
  """

  def create_model(self,
                   model_input,
                   vocab_size,
                   num_frames,
                   iterations=None,
                   add_batch_norm=None,
                   sample_random_frames=None,
                   cluster_size=None,
                   hidden_size=None,
                   is_training=True,
                   **unused_params):
    iterations = iterations or FLAGS.iterations
    add_batch_norm = add_batch_norm or FLAGS.dbof_add_batch_norm
    random_frames = sample_random_frames or FLAGS.sample_random_frames
    cluster_size = cluster_size or FLAGS.dbof_cluster_size
    hidden1_size = hidden_size or FLAGS.dbof_hidden_size

    num_frames = tf.cast(tf.expand_dims(num_frames, 1), tf.float32)
    if random_frames:
      model_input = utils.SampleRandomFrames(model_input, num_frames,
                                             iterations)
    else:
      model_input = utils.SampleRandomSequence(model_input, num_frames,
                                               iterations)
    max_frames = model_input.get_shape().as_list()[1]
    feature_size = model_input.get_shape().as_list()[2]
    reshaped_input = tf.reshape(model_input, [-1, feature_size])
    tf.summary.histogram("input_hist", reshaped_input)

    if add_batch_norm:
      reshaped_input = slim.batch_norm(
          reshaped_input,
          center=True,
          scale=True,
          is_training=is_training,
          scope="input_bn")

    cluster_weights = tf.Variable(tf.random_normal(
        [feature_size, cluster_size],
        stddev=1 / math.sqrt(feature_size)))
    tf.summary.histogram("cluster_weights", cluster_weights)
    activation = tf.matmul(reshaped_input, cluster_weights)
    if add_batch_norm:
      activation = slim.batch_norm(
          activation,
          center=True,
          scale=True,
          is_training=is_training,
          scope="cluster_bn")
    else:
      cluster_biases = tf.Variable(
          tf.random_normal(
              [cluster_size], stddev=1 / math.sqrt(feature_size)))
      tf.summary.histogram("cluster_biases", cluster_biases)
      activation += cluster_biases
    activation = tf.nn.relu6(activation)
    tf.summary.histogram("cluster_output", activation)

    activation = tf.reshape(activation, [-1, max_frames, cluster_size])
    activation = utils.FramePooling(activation, FLAGS.dbof_pooling_method)

    hidden1_weights = tf.Variable(tf.random_normal(
        [cluster_size, hidden1_size],
        stddev=1 / math.sqrt(cluster_size)))
    tf.summary.histogram("hidden1_weights", hidden1_weights)
    activation = tf.matmul(activation, hidden1_weights)
    if add_batch_norm:
      activation = slim.batch_norm(
          activation,
          center=True,
          scale=True,
          is_training=is_training,
          scope="hidden1_bn")
    else:
      hidden1_biases = tf.Variable(
          tf.random_normal(
              [hidden1_size], stddev=0.01))
      tf.summary.histogram("hidden1_biases", hidden1_biases)
      activation += hidden1_biases
    activation = tf.nn.relu6(activation)
    tf.summary.histogram("hidden1_output", activation)

    aggregated_model = getattr(video_level_models,
                               FLAGS.video_level_classifier_model)
    return aggregated_model().create_model(
        model_input=activation,
        vocab_size=vocab_size,
        **unused_params)

class LstmModel(models.BaseModel):

  def create_model(self, model_input, vocab_size, num_frames, **unused_params):
    """Creates a model which uses a stack of LSTMs to represent the video.

    Args:
      model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                   input features.
      vocab_size: The number of classes in the dataset.
      num_frames: A vector of length 'batch' which indicates the number of
           frames for each video (before padding).

    Returns:
      A dictionary with a tensor containing the probability predictions of the
      model in the 'predictions' key. The dimensions of the tensor are
      'batch_size' x 'num_classes'.
    """
    lstm_size = FLAGS.lstm_cells
    number_of_layers = FLAGS.lstm_layers

    ## Batch normalize the input
    stacked_lstm = tf.contrib.rnn.MultiRNNCell(
            [
                tf.contrib.rnn.BasicLSTMCell(
                    lstm_size, forget_bias=1.0, state_is_tuple=False)
                for _ in range(number_of_layers)
                ],
            state_is_tuple=False)

    with tf.variable_scope("RNN"):
      outputs, state = tf.nn.dynamic_rnn(stacked_lstm, model_input,
                                         sequence_length=num_frames,
                                         dtype=tf.float32)

    aggregated_model = getattr(video_level_models,
                               FLAGS.video_level_classifier_model)
    return aggregated_model().create_model(
        model_input=state,
        vocab_size=vocab_size,
        **unused_params)

class AttentionModel(models.BaseModel):

    def create_model(self, model_input, vocab_size, num_frames, **unused_params):
        """Creates a model which uses a stack of LSTMs to represent the video.

        Args:
          model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                       input features.
          vocab_size: The number of classes in the dataset.
          num_frames: A vector of length 'batch' which indicates the number of
               frames for each video (before padding).

        Returns:
          A dictionary with a tensor containing the probability predictions of the
          model in the 'predictions' key. The dimensions of the tensor are
          'batch_size' x 'num_classes'.
        """
        frames_sum = tf.reduce_sum(tf.abs(model_input),axis=2)
        frames_true = tf.ones(tf.shape(frames_sum))
        frames_false = tf.zeros(tf.shape(frames_sum))
        frames_bool = tf.where(tf.greater(frames_sum, frames_false), frames_true, frames_false)

        shape = model_input.get_shape().as_list()
        denominators = tf.reshape(
            tf.tile(tf.cast(tf.expand_dims(num_frames, 1), tf.float32), [1, shape[2]]), [-1, shape[2]])
        avg_pooled = tf.reduce_sum(model_input,
                               axis=[1]) / denominators
        avg_pooled = tf.tile(tf.reshape(avg_pooled,[-1,1,shape[2]]),[1,shape[1],1])

        attention_input = tf.reshape(tf.concat([model_input,avg_pooled],axis=2),[-1, shape[2]*2])

        with tf.variable_scope("Attention"):
            W = tf.Variable(tf.truncated_normal([shape[2]*2, 1], stddev=0.1), name="W")
            b = tf.Variable(tf.constant(0.1, shape=[1]), name="b")
            output = tf.nn.softmax(tf.nn.xw_plus_b(attention_input,W,b))
            output = tf.reshape(output,[-1,shape[1]])*frames_bool
            atten = output/tf.reduce_sum(output, axis=1, keep_dims=True)

        state = tf.reduce_sum(model_input*tf.reshape(atten,[-1,shape[1],1]),axis=1)


        aggregated_model = getattr(video_level_models,
                                   FLAGS.video_level_classifier_model)
        return aggregated_model().create_model(
            model_input=state,
            vocab_size=vocab_size,
            **unused_params)

class CnnModel(models.BaseModel):
    # highway layer that borrowed from https://github.com/carpedm20/lstm-char-cnn-tensorflow
    def highway(self, input_1, input_2, size_1, size_2, layer_size=1):
        """Highway Network (cf. http://arxiv.org/abs/1505.00387).

        t = sigmoid(Wy + b)
        z = t * g(Wy + b) + (1 - t) * y
        where g is nonlinearity, t is transform gate, and (1 - t) is carry gate.
        """
        output = input_2

        for idx in range(layer_size):
            with tf.name_scope('output_lin_%d' % idx):
                W = tf.Variable(tf.truncated_normal([size_2,size_1], stddev=0.1), name="W")
                b = tf.Variable(tf.constant(0.1, shape=[size_1]), name="b")
                output = tf.nn.relu(tf.nn.xw_plus_b(output,W,b))
            with tf.name_scope('transform_lin_%d' % idx):
                W = tf.Variable(tf.truncated_normal([size_1,size_1], stddev=0.1), name="W")
                b = tf.Variable(tf.constant(0.1, shape=[size_1]), name="b")
                transform_gate = tf.sigmoid(tf.nn.xw_plus_b(input_1,W,b))
            carry_gate = tf.constant(1.0) - transform_gate

            output = transform_gate * output + carry_gate * input_1

        return output

    def create_model(self, model_input, vocab_size, num_frames, **unused_params):
        """Creates a model which uses a stack of LSTMs to represent the video.

        Args:
          model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                       input features.
          vocab_size: The number of classes in the dataset.
          num_frames: A vector of length 'batch' which indicates the number of
               frames for each video (before padding).

        Returns:
          A dictionary with a tensor containing the probability predictions of the
          model in the 'predictions' key. The dimensions of the tensor are
          'batch_size' x 'num_classes'.
        """
        # Create a convolution + maxpool layer for each filter size
        filter_sizes = [1, 3, 5, 10, 20]
        #filter_sizes = [1]
        shape = model_input.get_shape().as_list()
        steps = []
        slices = []
        for i in range(len(filter_sizes)):
            step = filter_sizes[i]//2
            if i==0:
                step = 1
            steps.append(step)
            slice = ((shape[1]-step)//filter_sizes[i])*filter_sizes[i]
            slices.append([slice,slice+step])
        num_filters = [400, 300, 200, 100, 100]
        #num_filters = [200]
        pooled_outputs = []
        frames_sum = tf.reduce_sum(tf.abs(model_input),axis=2)
        frames_true = tf.ones(tf.shape(frames_sum))
        frames_false = tf.zeros(tf.shape(frames_sum))
        frames_bool = tf.where(tf.greater(frames_sum, frames_false), frames_true, frames_false)

        """
        with tf.variable_scope("CNN"):
            for filter_size, step, slice, num_filter in zip(filter_sizes, steps, slices, num_filters):
                with tf.name_scope("conv-maxpool-%s" % filter_size):
                    # Convolution Layer
                    num_step = slice[0]//filter_size
                    cnn_input_1 = tf.reshape(model_input[:, 0:slice[0], :], [-1, filter_size, num_step, shape[2]])
                    cnn_input_1 = tf.reshape(tf.transpose(cnn_input_1, perm=[0, 2, 1, 3]),[-1, filter_size*shape[2]])
                    cnn_input_2 = tf.reshape(model_input[:, step:slice[1], :], [-1, filter_size, num_step, shape[2]])
                    cnn_input_2 = tf.reshape(tf.transpose(cnn_input_2, perm=[0, 2, 1, 3]),[-1, filter_size*shape[2]])

                    frame_bool = frames_bool[:,0:slice[0]:filter_size]
                    frame_bool = tf.reshape(frame_bool/tf.reduce_sum(frame_bool,axis=1,keep_dims=True),[-1, num_step, 1])
                    filter_shape = [filter_size*shape[2], num_filter]
                    W = tf.Variable(tf.truncated_normal(filter_shape, stddev=0.1), name="W")
                    b = tf.Variable(tf.constant(0.1, shape=[num_filter]), name="b")
                    conv_1 = tf.tanh(tf.nn.xw_plus_b(cnn_input_1, W, b))
                    conv_2 = tf.tanh(tf.nn.xw_plus_b(cnn_input_2, W, b))
                    # Apply nonlinearity
                    conv_1 = tf.reshape(conv_1,[-1, num_step, num_filter])
                    conv_2 = tf.reshape(conv_2,[-1, num_step, num_filter])
                    conv = tf.concat([conv_1,conv_2], axis=1)*tf.concat([frame_bool,frame_bool], axis=1)
                    # Maxpooling over the outputs
                    pooled = tf.reduce_sum(conv, axis=1)
                    pooled_outputs.append(pooled)"""


        with tf.variable_scope("CNN"):

            num_frames = tf.cast(tf.expand_dims(num_frames, 1), tf.float32)
            denominators = tf.reshape(
                tf.tile(num_frames, [1, shape[2]]), [-1, shape[2]])
            avg_pooled = tf.reduce_sum(model_input,
                                       axis=[1]) / denominators
            """
            cnn_input = tf.reshape(model_input,[-1,shape[2]])
            filter_shape = [shape[2], shape[2]//8]
            W = tf.Variable(tf.truncated_normal(filter_shape, stddev=0.1), name="W")
            b = tf.Variable(tf.constant(0.1, shape=[shape[2]//8]), name="b")
            cnn_out = tf.tanh(tf.nn.xw_plus_b(cnn_input, W, b))
            cnn_input = tf.reshape(cnn_out,[-1,shape[1],shape[2]//8,1])
            shape = cnn_input.get_shape().as_list()"""
            cnn_input = tf.expand_dims(model_input, 3)

            for filter_size, step, num_filter in zip(filter_sizes, steps, num_filters):
                with tf.name_scope("conv-maxpool-%s" % filter_size):
                    # Convolution Layer
                    filter_shape = [filter_size, shape[2], 1, num_filter]
                    W = tf.Variable(tf.truncated_normal(filter_shape, stddev=0.1), name="W")
                    b = tf.Variable(tf.constant(0.1, shape=[num_filter]), name="b")
                    conv = tf.nn.conv2d(cnn_input,W,strides=[1, 1, 1, 1],padding="VALID",name="conv")
                    # Apply nonlinearity
                    h = tf.tanh(tf.nn.bias_add(conv, b), name="tanh")
                    # Maxpooling over the outputs
                    h_shape = h.get_shape().as_list()
                    h_out = tf.reshape(h,[-1,h_shape[1],num_filter])
                    #frame_bool = frames_bool[:,0:filter_size*h_shape[1]:filter_size]
                    frame_bool = frames_bool[:,0:h_shape[1]]
                    frame_bool = tf.reshape(frame_bool/tf.reduce_sum(frame_bool,axis=1,keep_dims=True),[-1, h_shape[1], 1])
                    # Maxpooling over the outputs
                    pooled = tf.reduce_sum(h_out*frame_bool, axis=1)
                    pooled_outputs.append(pooled)



            # Combine all the pooled features
            #num_filters_total = sum(num_filters)
            h_pool = tf.concat(pooled_outputs,1)
            #h_pool_flat = tf.reshape(h_pool, [-1, num_filters_total])


            """
            # Add highway
            with tf.name_scope("highway"):
                h_highway = self.highway(avg_pooled, h_pool_flat, shape[2], num_filters_total)

            # Add dropout
            with tf.name_scope("dropout"):
                h_drop = tf.nn.dropout(h_highway, 0.5)"""

            #h_drop = tf.concat([h_pool, avg_pooled], axis=1)
            h_drop = h_pool
            #h_drop = avg_pooled

        aggregated_model = getattr(video_level_models,
                                   FLAGS.video_level_classifier_model)
        return aggregated_model().create_model(
            model_input=h_drop,
            vocab_size=vocab_size,
            **unused_params)

class MixLogisticModel(models.BaseModel):

    def create_model(self, model_input, vocab_size, num_frames, **unused_params):
        """Creates a model which uses a logistic classifier over the average of the
        frame-level features.

        This class is intended to be an example for implementors of frame level
        models. If you want to train a model over averaged features it is more
        efficient to average them beforehand rather than on the fly.

        Args:
          model_input: A 'batch_size' x 'max_frames' x 'num_features' matrix of
                       input features.
          vocab_size: The number of classes in the dataset.
          num_frames: A vector of length 'batch' which indicates the number of
               frames for each video (before padding).

        Returns:
          A dictionary with a tensor containing the probability predictions of the
          model in the 'predictions' key. The dimensions of the tensor are
          'batch_size' x 'num_classes'.
        """
        feature_size = model_input.get_shape().as_list()[2]
        max_frames = model_input.get_shape().as_list()[1]

        frames_sum = tf.reduce_sum(tf.abs(model_input),axis=2)
        frames_true = tf.ones(tf.shape(frames_sum))
        frames_false = tf.zeros(tf.shape(frames_sum))
        frames_bool = tf.where(tf.greater(frames_sum, frames_false), frames_true, frames_false)
        #output_bool = tf.reshape(frames_bool,[-1,1])
        frames_bool = tf.reshape(frames_bool/tf.reduce_sum(frames_bool,axis=1,keep_dims=True),[-1,max_frames,1])

        reshaped_input = tf.reshape(model_input,[-1,feature_size])

        hidden = slim.fully_connected(
            reshaped_input, feature_size, activation_fn=tf.tanh,
            weights_regularizer=slim.l2_regularizer(1e-8))

        output = slim.fully_connected(
            hidden, vocab_size, activation_fn=tf.nn.sigmoid,
            weights_regularizer=slim.l2_regularizer(1e-8))

        videofeatures = tf.reduce_sum(tf.reshape(hidden,[-1,max_frames,feature_size])*frames_bool,axis=1)
        output = tf.reduce_sum(tf.reshape(output,[-1,max_frames,vocab_size])*frames_bool,axis=1)
        output = output/tf.reduce_sum(output,axis=1,keep_dims=True)

        aggregated_model = getattr(video_level_models,
                                   FLAGS.video_level_classifier_model)

        result = aggregated_model().create_model(model_input=videofeatures,vocab_size=vocab_size,**unused_params)
        result["predictions"] = (result["predictions"] + output)/tf.constant(2.0)
        return result