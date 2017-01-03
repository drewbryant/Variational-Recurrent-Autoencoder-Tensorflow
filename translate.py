# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/abs/1412.2007
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time
import logging
import json
import pdb

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import seq2seq_model
import h5py
from tensorflow.python.platform import gfile
from tensorflow.contrib.tensorboard.plugins import projector

default_args = {}
default_train_args = {}
default_model_args = {}
default_eval_args = {}

default_args['ckpt'] = "translate"

default_model_args['size'] = 128
default_model_args['num_layers'] = 1
default_model_args['latent_dim'] = 64
default_model_args['en_vocab_size'] = 10000
default_model_args['fr_vocab_size'] = 10000
default_model_args['data_dir'] = "corpus/line_based"
default_model_args['train_dir'] = "models"
default_model_args['dnn_in_between'] = True
default_model_args['batch_norm'] = False
default_model_args['use_lstm'] = False
default_model_args['mean_logvar_split'] = False
default_model_args['elu'] = True
default_model_args['buckets'] = "[0, 1, 2]"
default_model_args['beam_search'] = False
default_model_args['beam_size'] = 2

default_train_args['learning_rate'] = 0.001
default_train_args['kl_rate_rise_factor'] = 2
default_train_args['max_gradient_norm'] = 5.0
default_train_args['batch_size'] = 64
default_train_args['kl_rate_rise_time'] = 50000
default_train_args['Lambda'] = 2
default_train_args['latent_splits'] = default_model_args['latent_dim']
default_train_args['max_train_data_size'] = 0
default_train_args['steps_per_checkpoint'] = 2000
default_train_args['probabilistic'] = True
default_train_args['annealing'] = False
default_train_args['lower_bound_KL'] = True
default_train_args['feed_previous'] = True
default_train_args['word_dropout_keep_prob'] = 1.0

default_eval_args['input_file'] = "input.txt"
default_eval_args['num_pts'] = 3
default_eval_args['num_samples'] = 0
default_eval_args['decode'] = False
default_eval_args['self_test'] = False
default_eval_args['interpolate'] = False
default_eval_args['load_embeddings'] = False


tf.app.flags.DEFINE_float("learning_rate", 0.001, "Learning rate.")
tf.app.flags.DEFINE_float("Lambda", 2, "kl divergence threshold.")
tf.app.flags.DEFINE_float("kl_rate_rise_factor", 0.01,
                          "increase of kl rate per 200 step.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_float("word_dropout_keep_prob", 1.0,
                          "probability of decoder feeding previous output instead of UNK.")
tf.app.flags.DEFINE_integer("batch_size", 64,
                            "Batch size to use during training.")
tf.app.flags.DEFINE_integer("size", 128, "Size of each model layer.")
tf.app.flags.DEFINE_integer("kl_rate_rise_time", 50000, "when we start to increase our KL rate.")
tf.app.flags.DEFINE_integer("latent_splits", 64, "kl divergence latent splits.")
tf.app.flags.DEFINE_integer("num_layers", 1, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("latent_dim", 64, "latent dimension.")
tf.app.flags.DEFINE_integer("en_vocab_size", 10000, "English vocabulary size.")
tf.app.flags.DEFINE_integer("fr_vocab_size", 10000, "French vocabulary size.")
tf.app.flags.DEFINE_string("data_dir", "corpus/line_based", "Data directory")
tf.app.flags.DEFINE_string("train_dir", "models", "Training directory.")
tf.app.flags.DEFINE_string("ckpt", "translate", "checkpoint file name.")
tf.app.flags.DEFINE_string("input_file", "input.txt", "input file name.")
tf.app.flags.DEFINE_string("buckets", "[0,1,2]", "which buckets to use.")
tf.app.flags.DEFINE_integer("max_train_data_size", 0,
                            "Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("beam_size", 2,
                            "beam size for beam search.")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 2000,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_integer("num_pts", 3,
                            "Number of points between start point and end point.")
tf.app.flags.DEFINE_integer("num_samples", 0,
                            "Number of points between start point and end point.")
tf.app.flags.DEFINE_boolean("decode", False,
                            "Set to True for interactive decoding.")
tf.app.flags.DEFINE_boolean("self_test", False,
                            "Run a self-test if this is set to True.")
tf.app.flags.DEFINE_boolean("new", True,
                            "Train a new model.")
tf.app.flags.DEFINE_boolean("dnn_in_between", True,
                            "use dnn layer between encoder and decoder or not.")
tf.app.flags.DEFINE_boolean("probabilistic", True,
                            "use probabilistic layer or not.")
tf.app.flags.DEFINE_boolean("annealing", False,
                            "use kl cost annealing or not.")
tf.app.flags.DEFINE_boolean("elu", True,
                            "use elu or not. If False, use relu.")
tf.app.flags.DEFINE_boolean("lower_bound_KL", True,
                            "use lower bounded KL divergence or not.")
tf.app.flags.DEFINE_boolean("interpolate", False,
                            "set to True for interpolating.")
tf.app.flags.DEFINE_boolean("feed_previous", True,
                            "if True, inputs are feeded with last output.")
tf.app.flags.DEFINE_boolean("batch_norm", False,
                            "if True, use batch normalized LSTM.")
tf.app.flags.DEFINE_boolean("use_lstm", False,
                            "if True, use LSTM.")
tf.app.flags.DEFINE_boolean("mean_logvar_split", False,
                            "True is deprecated and will soon be removed.")
tf.app.flags.DEFINE_boolean("beam_search", False,
                            "use beam search or not.")
tf.app.flags.DEFINE_boolean("load_embeddings", False,
                            "load pre trained embeddings or not.")

FLAGS = tf.app.flags.FLAGS




# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
_buckets = [(8, 10), (33, 35), (65, 67)]

_buckets = [_buckets[i] for i in json.loads(FLAGS.buckets)]


def read_data(source_path, target_path, max_size=None):
  """Read data from source and target files and put into buckets.

  Args:
    source_path: path to the files with token-ids for the source language.
    target_path: path to the file with token-ids for the target language;
      it must be aligned with the source file: n-th line contains the desired
      output for n-th line from the source_path.
    max_size: maximum number of lines to read, all other will be ignored;
      if 0 or None, data files will be read completely (no limit).

  Returns:
    data_set: a list of length len(_buckets); data_set[n] contains a list of
      (source, target) pairs read from the provided data files that fit
      into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
      len(target) < _buckets[n][1]; source and target are lists of token-ids.
  """
  data_set = [[] for _ in _buckets]
  with tf.gfile.GFile(source_path, mode="r") as source_file:
    with tf.gfile.GFile(target_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target and (not max_size or counter < max_size):
        counter += 1
        if counter % 100000 == 0:
          print("  reading data line %d" % counter)
          sys.stdout.flush()
        source_ids = [int(x) for x in source.split()]
        target_ids = [int(x) for x in target.split()]
        target_ids.append(data_utils.EOS_ID)
        for bucket_id, (source_size, target_size) in enumerate(_buckets):
          if len(source_ids) < source_size and len(target_ids) < target_size:
            data_set[bucket_id].append([source_ids, target_ids])
            break
        source, target = source_file.readline(), target_file.readline()
  return data_set


def create_model(session, forward_only):
  """Create translation model and initialize or load parameters in session."""
  dtype = tf.float32
  optimizer = tf.train.AdamOptimizer(FLAGS.learning_rate)
  activation = tf.nn.elu if FLAGS.elu else tf.nn.relu
  model = seq2seq_model.Seq2SeqModel(
      FLAGS.en_vocab_size,
      FLAGS.fr_vocab_size,
      _buckets,
      FLAGS.size,
      FLAGS.num_layers,
      FLAGS.latent_dim,
      FLAGS.max_gradient_norm,
      FLAGS.batch_size,
      FLAGS.learning_rate,
      FLAGS.latent_splits,
      FLAGS.Lambda,
      FLAGS.word_dropout_keep_prob,
      FLAGS.beam_search,
      FLAGS.beam_size,
      FLAGS.annealing,
      FLAGS.lower_bound_KL,
      FLAGS.kl_rate_rise_time,
      FLAGS.kl_rate_rise_factor,
      FLAGS.use_lstm,
      FLAGS.mean_logvar_split,
      FLAGS.load_embeddings,
      optimizer=optimizer,
      activation=activation,
      dnn_in_between=FLAGS.dnn_in_between,
      probabilistic=FLAGS.probabilistic,
      batch_norm=FLAGS.batch_norm,
      forward_only=forward_only,
      feed_previous=FLAGS.feed_previous,
      dtype=dtype)
  print(FLAGS.model_dir)
  ckpt = tf.train.get_checkpoint_state(FLAGS.model_dir)
  if not FLAGS.new and ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
    print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    print("Created model with fresh parameters.")
    session.run(tf.global_variables_initializer())
  return model

def train(stats):
  """Train a en->fr translation model using WMT data."""
  # Prepare WMT data.
  print("Preparing WMT data in %s" % FLAGS.data_dir)
  en_train, fr_train, en_dev, fr_dev, _, _ = data_utils.prepare_wmt_data(
      FLAGS.data_dir, FLAGS.en_vocab_size, FLAGS.fr_vocab_size)

  with tf.Session() as sess:
    if not os.path.exists(FLAGS.model_dir):
      os.makedirs(FLAGS.model_dir)
    train_writer = tf.summary.FileWriter(FLAGS.model_dir+ "/train", graph=sess.graph)
    dev_writer = tf.summary.FileWriter(FLAGS.model_dir + "/test", graph=sess.graph)


    stat_file_name = "stats/" + FLAGS.ckpt + ".json" 
    print(FLAGS.__dict__['__flags'])
    # Create model.
    print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
    model = create_model(sess, False)

    # Read data into buckets and compute their sizes.
    print ("Reading development and training data (limit: %d)."
           % FLAGS.max_train_data_size)
    dev_set = read_data(en_dev, fr_dev)
    train_set = read_data(en_train, fr_train, FLAGS.max_train_data_size)
    train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
    train_total_size = float(sum(train_bucket_sizes))

    # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
    # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
    # the size if i-th training bucket, as used later.
    train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                           for i in xrange(len(train_bucket_sizes))]
    if FLAGS.load_embeddings:
      with h5py.File(FLAGS.data_dir + "/vocab{0}".format(FLAGS.en_vocab_size) + '.en.embeddings.h5','r') as h5f:
        enc_embeddings = h5f['embeddings'][:]
      sess.run(model.enc_embedding_init_op, feed_dict={model.enc_embedding_placeholder: enc_embeddings})
      del enc_embeddings
      with h5py.File(FLAGS.data_dir + "/vocab{0}".format(FLAGS.fr_vocab_size) + '.fr.embeddings.h5','r') as h5f:
        dec_embeddings = h5f['embeddings'][:]
      sess.run(model.dec_embedding_init_op, feed_dict={model.dec_embedding_placeholder: dec_embeddings})
      del dec_embeddings

    # This is the training loop.
    step_time, loss = 0.0, 0.0
    KL_loss = 0.0
    current_step = model.global_step.eval()
    step_loss_summaries = []
    step_KL_loss_summaries = []
    overall_start_time = time.time()
    while True:
      # Choose a bucket according to data distribution. We pick a random number
      # in [0, 1] and use the corresponding interval in train_buckets_scale.
      random_number_01 = np.random.random_sample()
      bucket_id = min([i for i in xrange(len(train_buckets_scale))
                       if train_buckets_scale[i] > random_number_01])

      # Get a batch and make a step.
      start_time = time.time()
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          train_set, bucket_id)
      _, step_loss, step_KL_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                   target_weights, bucket_id, False)
      step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
      step_loss_summaries.append(tf.Summary(value=[tf.Summary.Value(tag="step loss", simple_value=float(step_loss))]))
      step_KL_loss_summaries.append(tf.Summary(value=[tf.Summary.Value(tag="KL step loss", simple_value=float(step_KL_loss))]))
      loss += step_loss / FLAGS.steps_per_checkpoint
      KL_loss += step_KL_loss / FLAGS.steps_per_checkpoint
      current_step = model.global_step.eval()

      # Once in a while, we save checkpoint, print statistics, and run evals.
      if current_step % FLAGS.steps_per_checkpoint == 0:
        # Print statistics for the previous epoch.
        perplexity = math.exp(float(loss)) if loss < 300 else float("inf")
        print ("global step %d learning rate %.4f step-time %.2f perplexity "
               "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                         step_time, perplexity))

        print ("global step %d learning rate %.4f step-time %.2f KL divergence "
               "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                         step_time, KL_loss))
        wall_time = time.time() - overall_start_time
        print("time passed: {0}".format(wall_time))
        stats['wall_time'][str(current_step)] = wall_time

        # Add perplexity, KL divergence to summary and stats.
        perp_summary = tf.Summary(value=[tf.Summary.Value(tag="train perplexity", simple_value=perplexity)])
        train_writer.add_summary(perp_summary, current_step)
        KL_loss_summary = tf.Summary(value=[tf.Summary.Value(tag="KL divergence", simple_value=KL_loss)])
        train_writer.add_summary(KL_loss_summary, current_step)
        for i, summary in enumerate(step_loss_summaries):
          train_writer.add_summary(summary, current_step - 200 + i)
        step_loss_summaries = []
        for i, summary in enumerate(step_KL_loss_summaries):
          train_writer.add_summary(summary, current_step - 200 + i)
        step_KL_loss_summaries = []

        stats['train_perplexity'][str(current_step)] = perplexity
        stats['train_KL_divergence'][str(current_step)] = KL_loss

        if FLAGS.annealing:
          if current_step >= FLAGS.kl_rate_rise_time and model.kl_rate.eval() < 1:
            sess.run(model.kl_rate_rise_op)


        # Save checkpoint and zero timer and loss.
        checkpoint_path = os.path.join(FLAGS.model_dir, FLAGS.ckpt + ".ckpt")
        model.saver.save(sess, checkpoint_path, global_step=model.global_step)
        step_time, loss, KL_loss = 0.0, 0.0, 0.0

        # Run evals on development set and print their perplexity.
        eval_losses = []
        eval_KL_losses = []
        eval_bucket_num = 0
        for bucket_id in xrange(len(_buckets)):
          if len(dev_set[bucket_id]) == 0:
            print("  eval: empty bucket %d" % (bucket_id))
            continue
          eval_bucket_num += 1
          encoder_inputs, decoder_inputs, target_weights = model.get_batch(
              dev_set, bucket_id)
          _, eval_loss, eval_KL_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                       target_weights, bucket_id, True)
          eval_losses.append(float(eval_loss))
          eval_KL_losses.append(float(eval_KL_loss))
          eval_ppx = math.exp(float(eval_loss)) if eval_loss < 300 else float(
              "inf")
          print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))

          eval_perp_summary = tf.Summary(value=[tf.Summary.Value(tag="eval perplexity for bucket {0}".format(bucket_id), simple_value=eval_ppx)])
          dev_writer.add_summary(eval_perp_summary, current_step)

        mean_eval_loss = sum(eval_losses) / float(eval_bucket_num)
        mean_eval_KL_loss = sum(eval_KL_losses) / float(eval_bucket_num)
        mean_eval_ppx = math.exp(float(mean_eval_loss))
        print("  eval: mean perplexity {0}".format(mean_eval_ppx))

        stats['eval_perplexity'][str(current_step)] = mean_eval_ppx
        stats['eval_KL_divergence'][str(current_step)] = mean_eval_KL_loss
        eval_loss_summary = tf.Summary(value=[tf.Summary.Value(tag="mean eval loss", simple_value=float(mean_eval_ppx))])
        dev_writer.add_summary(eval_loss_summary, current_step)
        eval_KL_loss_summary = tf.Summary(value=[tf.Summary.Value(tag="mean eval loss", simple_value=float(mean_eval_KL_loss))])
        dev_writer.add_summary(eval_KL_loss_summary, current_step)
        with open(stat_file_name, "w") as statfile:
          statfile.write(json.dumps(stats))
        sys.stdout.flush()


def autoencode():
  with tf.Session() as sess:
    # Create model and load parameters.
    model = create_model(sess, True)
    model.batch_size = 1  # We decode one sentence at a time.

    # Load vocabularies.
    en_vocab_path = os.path.join(FLAGS.data_dir,
                                 "vocab%d.en" % FLAGS.en_vocab_size)
    fr_vocab_path = os.path.join(FLAGS.data_dir,
                                 "vocab%d.fr" % FLAGS.fr_vocab_size)
    en_vocab, _ = data_utils.initialize_vocabulary(en_vocab_path)
    _, rev_fr_vocab = data_utils.initialize_vocabulary(fr_vocab_path)

    # Decode from standard input.
    sys.stdout.write("> ")
    sys.stdout.flush()
    with gfile.GFile(FLAGS.input_file, "r") as fs:
      sentences = fs.readlines()
    with gfile.GFile(FLAGS.ckpt + ".output.txt", "w") as fo:
      for i, sentence in  enumerate(sentences):
        # Get token-ids for the input sentence.
        token_ids = data_utils.sentence_to_token_ids(sentence, en_vocab)
        # Which bucket does it belong to?
        bucket_id = len(_buckets) - 1
        for i, bucket in enumerate(_buckets):
          if bucket[0] >= len(token_ids):
            bucket_id = i
            break
        else:
          logging.warning("Sentence truncated: %s", sentence) 

        # Get a 1-element batch to feed the sentence to the model.
        encoder_inputs, decoder_inputs, target_weights = model.get_batch(
            {bucket_id: [(token_ids, [])]}, bucket_id)
        # Get output logits for the sentence.
        _, _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                         target_weights, bucket_id, True)
        # This is a greedy decoder - outputs are just argmaxes of output_logits.
        outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]
        # If there is an EOS symbol in outputs, cut them at that point.
        if data_utils.EOS_ID in outputs:
          outputs = outputs[:outputs.index(data_utils.EOS_ID)]
        # Print out French sentence corresponding to outputs.
          fo.write(" ".join([rev_fr_vocab[output] for output in outputs]) + "\n")


def encode(sess, model, sentences):
  # Load vocabularies.
  en_vocab_path = os.path.join(FLAGS.data_dir,
                               "vocab%d.en" % FLAGS.en_vocab_size)
  fr_vocab_path = os.path.join(FLAGS.data_dir,
                               "vocab%d.fr" % FLAGS.fr_vocab_size)
  en_vocab, _ = data_utils.initialize_vocabulary(en_vocab_path)
  _, rev_fr_vocab = data_utils.initialize_vocabulary(fr_vocab_path)
  
  means = []
  logvars = []
  for i, sentence in enumerate(sentences):
    # Get token-ids for the input sentence.
    token_ids = data_utils.sentence_to_token_ids(sentence, en_vocab)
    # Which bucket does it belong to?
    bucket_id = len(_buckets) - 1
    for i, bucket in enumerate(_buckets):
      if bucket[0] >= len(token_ids):
        bucket_id = i
        break
    else:
      logging.warning("Sentence truncated: %s", sentence) 

        # Get a 1-element batch to feed the sentence to the model.
    encoder_inputs, _, _ = model.get_batch(
        {bucket_id: [(token_ids, [])]}, bucket_id)
    # Get output logits for the sentence.
    mean, logvar = model.encode_to_latent(sess, encoder_inputs, bucket_id)
    means.append(mean)
    logvars.append(logvar)

  return means, logvars


def decode(sess, model, means, logvars, bucket_id):
  fr_vocab_path = os.path.join(FLAGS.data_dir,
                               "vocab%d.fr" % FLAGS.fr_vocab_size)
  _, rev_fr_vocab = data_utils.initialize_vocabulary(fr_vocab_path)

  _, decoder_inputs, target_weights = model.get_batch(
      {bucket_id: [([], [])]}, bucket_id)
  outputs = []
  for mean, logvar in zip(means, logvars):
    mean = mean.reshape(1,-1)
    logvar = logvar.reshape(1,-1)
    output_logits = model.decode_from_latent(sess, mean, logvar, bucket_id, decoder_inputs, target_weights)
    output = [int(np.argmax(logit, axis=1)) for logit in output_logits]
    # If there is an EOS symbol in outputs, cut them at that point.
    if data_utils.EOS_ID in output:
      output = output[:output.index(data_utils.EOS_ID)]
    output = " ".join([rev_fr_vocab[word] for word in output]) + "\n"
    outputs.append(output)

  return outputs
  # Print out French sentence corresponding to outputs.

def n_sample(sess, model, sentence, num_sample):
  mean, logvar = encode(sess, model, [sentence])
  mean = mean[0][0][0]
  logvar = logvar[0][0][0]
  means = [mean] * num_sample
  zero_logvar = np.zeros(shape=logvar.shape)
  logvars = [zero_logvar] + [logvar] * (num_sample - 1)
  outputs = decode(sess, model, means, logvars, len(_buckets) - 1)
  with gfile.GFile(FLAGS.ckpt + ".{0}_sample.txt".format(num_sample), "w") as fo:
    for output in outputs:
      fo.write(output)
  

def interpolate(sess, model, means, logvars, num_pts):
  if len(means) != 2:
    raise ValueError("there should be two sentences when interpolating."
                     "number of setences: %d." % len(means))
  if num_pts < 3:
    raise ValueError("there should be more than two points when interpolating."
                     "number of points: %d." % num_pts)
  pts = []
  for s, e in zip(means[0][0][0].tolist(),means[1][0][0].tolist()):
    pts.append(np.linspace(s, e, num_pts))


  pts = np.array(pts)
  pts = pts.T
  pts = [np.array(pt) for pt in pts.tolist()]
  bucket_id = len(_buckets) - 1
  with gfile.GFile(FLAGS.ckpt + ".interpolate.txt", "w") as fo:
    logvars = [np.zeros(shape=pt.shape) for pt in pts]
    outputs = decode(sess, model, pts, logvars, bucket_id)
    for output in outputs:
      fo.write(output)



def self_test():
  """Test the translation model."""
  with tf.Session() as sess:
    print("Self-test for neural translation model.")
    # Create model with vocabularies of 10, 2 small buckets, 2 layers of 32.
    model = seq2seq_model.Seq2SeqModel(10, 10, [(3, 3), (6, 6)], 32, 2,
                                       5.0, 32, 0.3, 0.99, num_samples=8)
    sess.run(tf.global_variables_initializer())

    # Fake data set for both the (3, 3) and (6, 6) bucket.
    data_set = ([([1, 1], [2, 2]), ([3, 3], [4]), ([5], [6])],
                [([1, 1, 1, 1, 1], [2, 2, 2, 2, 2]), ([3, 3, 3], [5, 6])])
    for _ in xrange(5):  # Train the fake model for 5 steps.
      bucket_id = random.choice([0, 1])
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          data_set, bucket_id)
      model.step(sess, encoder_inputs, decoder_inputs, target_weights,
                 bucket_id, False)


def main(_):

  FLAGS.model_dir = FLAGS.train_dir + "/" + FLAGS.ckpt
  stat_file_name = "stats/" + FLAGS.ckpt + ".json" 
  if FLAGS.new:
    if os.path.exists(stat_file_name):
      print("error: create an already existed statistics file")
      sys.exit()
    stats = {}
    stats['hyperparameters'] = FLAGS.__dict__['__flags']
    stats['model_name'] = stats['hyperparameters']['ckpt']
    stats['train_perplexity'] = {}
    stats['train_KL_divergence'] = {}
    stats['eval_KL_divergence'] = {}
    stats['eval_perplexity'] = {}
    stats['wall_time'] = {}
    with open(stat_file_name, "w") as statfile:
      statfile.write(json.dumps(stats))
  else:
    with open(stat_file_name, "r") as statfile:
      statjson = statfile.read()
      stats = json.loads(statjson)
      hparams = stats['hyperparameters']
      for key, _ in default_model_args.items():
        FLAGS.__dict__['__flags'][key] = hparams.get(key, default_model_args[key])
      samekeys = [k for k in default_train_args if default_train_args[k] == FLAGS.__dict__['__flags'][k]]
      for key in samekeys:
        FLAGS.__dict__['__flags'][key] = hparams.get(key, default_train_args[key])

  if FLAGS.self_test:
    self_test()
  elif FLAGS.decode:
    autoencode()
  elif FLAGS.interpolate:
    with tf.Session() as sess:
      model = create_model(sess, True)
      with gfile.GFile(FLAGS.input_file, "r") as fs:
        sentences = fs.readlines()
      model.batch_size = 1
      means, logvars = encode(sess, model, sentences)
      interpolate(sess, model, means, logvars, FLAGS.num_pts)
  elif FLAGS.num_samples > 0:
    with tf.Session() as sess:
      model = create_model(sess, True)
      with gfile.GFile(FLAGS.input_file, "r") as fs:
        sentence = fs.readline()
      model.batch_size = 1
      n_sample(sess, model, sentence, FLAGS.num_samples)
    
  else:
    train(stats)

if __name__ == "__main__":
  tf.app.run()
