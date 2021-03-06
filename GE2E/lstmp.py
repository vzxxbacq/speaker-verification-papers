import sys
sys.path.append('../')
import logging
from pyasv.basic import model
from pyasv import layers
from pyasv import Config
from pyasv import ops
from pyasv import utils
from pyasv import loss
from pyasv.speech import pad
import numpy as np
import h5py
from scipy.spatial.distance import cdist
import tensorflow as tf
import os
import time


class LSTMP(model.Model):
    """GE2E Loss model based on LSTM."""
    def __init__(self, config, lstm_units, layer_num, dropout_prob):
        """
        :param config: ``Config``
        :param lstm_units: Hidden layer unit of LSTM.
        :param layer_num: Number of LSTM layers.
        :param dropout_prob: probability of dropout layer for input.
        """
        super().__init__(config)
        self.units = lstm_units
        self._feature = None  # define in self.inference
        self._score = None    # define in self.loss
        self.layer_num = layer_num
        self.logger = logging.getLogger('train')
        self.embed_size = lstm_units
        self.batch_size = self.config.num_classes_per_batch * self.config.num_classes_per_batch
        self.n_speaker_test = config.n_speaker_test
        self.drop_prob = dropout_prob
        self.data_shape = [None, 
                           (self.config.fix_len * self.config.sample_rate) // self.config.hop_length, 
                           self.config.feature_dims]

    @property
    def feature(self):
        """operation to get embeddings."""
        if self._score: return self._feature
        else:  self.logger.error("Can't use `feature` property before use inference.")

    @property
    def score_mat(self):
        """operation to get score matrix."""
        if self._score: return self._score
        else: self.logger.error("Can't use `score_mat` property before call `self.loss`.")

    def inference(self, x, is_training=True):
        """inference operation.
        :param x: input of model. should be tensor or placeholder.
        :param is_training: bool, set dropout while training.
        """
        x = tf.transpose(x, [1, 0, 2])
        if self.drop_prob > 0:
            x = tf.nn.dropout(x, rate=self.drop_prob)
        with tf.variable_scope('Forward', reuse=tf.AUTO_REUSE):
            with tf.variable_scope('LSTM', reuse=tf.AUTO_REUSE):
                outputs, _ = layers.lstm(x, self.units, is_training, self.layer_num)
            self._feature = outputs[-1]
            out = ops.normalize(outputs[-1])
        return out

    def loss(self, embeddings, loss_type='softmax'):
        with tf.variable_scope("loss", reuse=tf.AUTO_REUSE):
            b = tf.get_variable(name='loss_b', shape=[], dtype=tf.float32, initializer=tf.constant_initializer(-5.0))
            w = tf.get_variable(name='loss_w', shape=[], dtype=tf.float32, initializer=tf.constant_initializer(10.0))    
            embeddings = tf.reshape(embeddings, [self.config.num_classes_per_batch, 
                                                 self.config.num_utt_per_class,
                                                 self.embed_size])
            return loss.generalized_end_to_end_loss(embeddings, w=w, b=b)

    def summary(self):
        tf.summary.scalar('loss', self.get_tensor("ave_loss"))
        tf.summary.scalar('w', self.get_tensor("loss/loss_w"))
        tf.summary.scalar('b', self.get_tensor("loss/loss_b"))
        summary_op = tf.summary.merge_all()
        return summary_op

    def train(self, train_data, valid=None):
        """Interface to train model.
        
        :param train_data: `tf.data.dataset`
        :param valid: dict, defaults to None. contain enroll and test data like {'t_x:0': [...], 'e_x:0': [...], 'e_y:0': ...}
        """
        logger = logging.getLogger('train')
        tf_config = tf.ConfigProto(allow_soft_placement=True)
        tf_config.gpu_options.allow_growth = True
        sess = tf.Session(config=tf_config)
        opt = tf.train.AdamOptimizer(learning_rate=self.config.lr)
        logger.info('Build model on %s tower...'%('cpu' if self.config.n_gpu == 0 else 'gpu'))
        tower_y, tower_losses, tower_grads, tower_output = [], [], [], []
        for gpu_id in range(self.config.n_gpu):
            with tf.device('/gpu:%d' % gpu_id):
                x, y = train_data.get_next()
                output = self.inference(x)
                tower_output.append(output)
                losses = self.loss(output)
                tower_losses.append(losses)
                grads = ops.clip_grad_by_value(opt.compute_gradients(losses), -3.0, 3.0)
                grads = [(0.01 * i, j) if (j.name == 'loss/loss_b:0' or j.name == 'loss/loss_w:0') else (i, j) for i, j in grads]
                tower_grads.append(grads)
        # handle batch loss
        aver_loss_op = tf.reduce_mean(tower_losses, name='ave_loss')
        apply_gradient_op = opt.apply_gradients(ops.average_gradients(tower_grads))
        all_output = tf.reshape(tf.stack(tower_output, 0), [-1, self.embed_size])

        summary_op = self.summary()
        # init
        emb = self.init_validation()
        sess.run(tf.global_variables_initializer())
        saver = tf.train.Saver()


        summary_writer = tf.summary.FileWriter(os.path.join(self.config.save_path, 'graph'), sess.graph)
        log_flag = 0
        
        for epoch in range(self.config.max_step):
            start_time = time.time()
            logger.info('Epoch:%d, lr:%.4f, total_batch=%d' %
                        (epoch, self.config.lr, self.config.batch_nums_per_epoch))
            avg_loss = 0.0
            for batch_idx in range(self.config.batch_nums_per_epoch):
                _, _loss, _, summary_str = sess.run([apply_gradient_op, aver_loss_op, all_output, summary_op])

                avg_loss += _loss
                log_flag += 1
                
                if log_flag % 300 == 0 and log_flag != 0:
                    duration = time.time() - start_time
                    start_time = time.time()
                    logger.info('At %d batch, present batch loss is %.4f, %.2f batches/sec' %
                                (batch_idx, _loss, 300 * self.config.n_gpu / duration))
                if log_flag % 5000 == 0 and log_flag != 0:
                    test_x, test_y, enroll_x, enroll_y = valid['t_x'], valid['t_y'], valid['e_x'], valid['e_y']
                    acc, tup = self._validation(emb, test_x, test_y, enroll_x, enroll_y, sess, step=epoch)
                    _, emb_arr, label, _ = tup
                    utils.tensorboard_embedding(self.config.save_path, summary_writer, emb=emb_arr, label=label)
                    logger.info('At %d epoch after %d batch, acc is %.6f'
                                % (epoch, batch_idx, acc))
                summary_writer.add_summary(summary_str, epoch * self.config.batch_nums_per_epoch + batch_idx)
            avg_loss /= self.config.batch_nums_per_epoch
            logger.info('Train average loss:%.4f' % avg_loss)
            abs_save_path = os.path.abspath(os.path.join(self.config.save_path, 'model',
                                                         self.config.model_name + ".ckpt"))
            saver.save(sess=sess, save_path=abs_save_path)
        logger.info('training done.')

    def _validation(self, emb, test_x, test_y, enroll_x, enroll_y, sess, limit_shape=64, step=0):
        idx = np.random.randint(0, test_x.shape[0], size=limit_shape)
        test_x = test_x.value[idx]
        test_y = test_y[idx]
        test_y_uni = np.unique(test_y)
        enroll_x = np.array(list(enroll_x.value[np.where(enroll_y == i)[0]] for i in test_y_uni)).reshape(-1, self.config.sample_rate * self.config.fix_len)
        enroll_y = np.array(list(enroll_y[np.where(enroll_y == i)[0]] for i in test_y_uni)).reshape(-1,)
        t_emb = sess.run(emb, feed_dict={"t_x:0": test_x})
        e_emb = sess.run(emb, feed_dict={"t_x:0": enroll_x})
        spkr_embeddings = np.array([np.mean(e_emb[enroll_y.reshape(-1,) == i], 0)
                                    for i in range(self.n_speaker_test)], dtype=np.float32)
        score_mat = np.array([np.reshape(1 - cdist(spkr_embeddings[i].reshape(1, 400), t_emb, metric='cosine'), (-1, ))
                              for i in range(self.n_speaker_test)]).T
        ind = np.where(np.isnan(score_mat))
        score_mat[ind] = -1
        score_idx = np.argmax(score_mat, -1)
        return np.sum(score_idx == test_y.reshape(-1,)) / score_idx.shape[0], (score_mat, e_emb, enroll_y, test_y)

    def init_validation(self):
        """Get validation operation."""
        inp = tf.placeholder(dtype=tf.float32, shape=self.data_shape, name='t_x')
        # score_mat = self._valid(p_test_x, p_enroll_x, p_enroll_y)
        emb = self.inference(inp)
        return emb

    def predict(self, data, model_dir):
        with tf.Session() as sess:
            def batched_array(x):
                l = (x.shape[0] // 64 + 1) * 64
                x = pad(x, length=l, axis=0, mode='repeat')
                if len(x.shape) == 2:
                    x = x.reshape([(x.shape[0] // 64 + 1) if x.shape[0] % 64 != 0 else x.shape[0] // 64, 64, x.shape[1]])
                else:
                    x = x.reshape([(x.shape[0] // 64 + 1) if x.shape[0] % 64 != 0 else x.shape[0] // 64, 
                                   64, x.shape[1], x.shape[2]])          
                return x

            emb = self.init_validation()
            saver = tf.train.Saver()
            saver.restore(sess, model_dir)
            test_x, test_y, enroll_x, enroll_y = data['t_x'], data['t_y'], data['e_x'], data['e_y']
            test_x = batched_array(test_x.value)
            test_y = test_y.reshape(-1, 1)
            test_y = batched_array(test_y).reshape(-1,)
            enroll_x = batched_array(enroll_x)
            enroll_y = enroll_y.reshape(-1, 1)
            enroll_y = batched_array(enroll_y)

            # Enroll. 
            number_of_enrollment = np.zeros(shape=[self.config.n_speaker_test])
            sum_of_vector = np.zeros(shape=[self.config.n_speaker_test, self.embed_size], dtype=np.float32)
            for x, y in zip(enroll_x, enroll_y):
                e_emb = sess.run(emb, feed_dict={"t_x:0": x})
                for i in range(self.config.n_speaker_test):
                    number_of_enrollment[i] += np.sum(y == i)
                    sum_of_vector[i] += np.sum(e_emb[np.where(y == i)[0]], axis=0)
            
            score_mat = None
            for x in test_x:
                t_emb = sess.run(emb, feed_dict={"t_x:0": x})
                score_mat_batch = 1 - cdist(t_emb, sum_of_vector, metric='cosine')
                if score_mat is None:
                    score_mat = score_mat_batch
                else:
                    score_mat = np.concatenate([score_mat, score_mat_batch], axis=0)
            score_idx = np.argmax(score_mat, -1)
            acc = np.sum(score_idx == test_y) / score_idx.shape[0]
            eer = utils.calc_eer(score_mat, test_y,
                                 save_path=os.path.join(self.config.save_path, 'graph', 'eer.png'))
            self.logger.info("acc: %.6f \teer: %.6f" % (acc, eer))
            return acc, eer

