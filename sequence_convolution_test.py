# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import unittest
import random
import numpy as np
import hebel
if not hebel.is_initialized:
    hebel.init()

from pycuda import driver
from sequence_convolution.pycuda_ops import convolve_sequence, convolve_1d, \
     convolve_sequence_gradient, convolve_1d_gradient_filters, \
     max_pool, max_pool_gradient, \
     sum_pool, sum_pool_gradient, convolve_1d_gradient_input
from pycuda import gpuarray
from pycuda.curandom import rand as curand
from sequence_convolution.seq_array import encode_sequence, sample_sequence
from sequence_convolution.models import SequenceConvolutionNet, \
     SequenceConvolutionLayer, MultiSequenceConvolutionLayer, MaxPoolingLayer
from sequence_convolution.seq_array import SeqArrayDataProvider, sample_sequence, \
    encode_sequence
from hebel.data_providers import MiniBatchDataProvider
from hebel.optimizers import SGD
from hebel.schedulers import constant_scheduler
from hebel.parameter_updaters import SimpleSGDUpdate
from hebel.monitors import SimpleProgressMonitor
from copy import copy, deepcopy
from itertools import izip, product

def checkgrad_model(layer, input_data, epsilon=1e-4, **kwargs):
    cache = layer.feed_forward(input_data)
    f0 = np.sum(cache[0].get())
    df_output = gpuarray.empty_like(cache[0]).fill(1.)
    grads = layer.backprop(input_data, df_output, cache)[0]
    dtype = grads[0].dtype
    eps = np.finfo(dtype).eps

    grad_approx = [np.empty_like(p.get()) for p in layer.parameters]
    loss = 0

    parameters = layer.parameters
    for i in range(len(layer.parameters)):
        param_i = parameters[i].get()
        grad_approx_i = grad_approx[i]

        assert param_i.shape == grad_approx_i.shape

        for idx, _ in np.ndenumerate(grad_approx_i):
            p = list(copy(parameters))
            w0 = param_i[idx]

            # Get f(x - epsilon)
            param_i[idx] += epsilon
            p[i] = gpuarray.to_gpu(param_i)
            layer.parameters = p
            f1 = np.sum(layer.feed_forward(input_data)[0].get())

            # Get f(x + epsilon)
            param_i[idx] -= 2 * epsilon
            p[i] = gpuarray.to_gpu(param_i)
            layer.parameters = p
            f2 = np.sum(layer.feed_forward(input_data)[0].get())

            # Reset weight
            param_i[idx] = w0
            p[i] = gpuarray.to_gpu(param_i)
            layer.parameters = p

            # Compute gradient approximation
            grad_approx_i[idx] = (f1 - f2) / (2 * epsilon)

        loss += np.sum(((grads[i].get() - grad_approx_i) /
                        (grads[i].get() + eps)) ** 2.)
    loss = np.sqrt(loss) / np.sum([g.size for g in grads])

    return loss


class TestSeqConvolution(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-4
    DOUBLE_ERR_TOL = 1e-13

    @staticmethod
    def cpu_conv_seq(x, w, b):
        height, width = x.shape
        n_filters = w.shape[0]
        filter_width = w.shape[1]
        output_width = width - filter_width + 1

        y = np.empty((height, n_filters, output_width), dtype=w.dtype)

        for f in range(n_filters):
            for j in range(output_width):
                y[:, f, j] = b[f]
                for k in range(filter_width):
                    nt = x[:, j + k]
                    y[np.bool_(nt == 'A'), f, j] += w[f, k, 0]
                    y[np.bool_(nt == 'C'), f, j] += w[f, k, 1]
                    y[np.bool_(nt == 'G'), f, j] += w[f, k, 2]
                    y[np.bool_(nt == 'T'), f, j] += w[f, k,  3]
                    y[np.bool_(nt == 'R'), f, j] += \
                        .5 * w[f, k, 0] + .5 * w[f, k, 2]
                    y[np.bool_(nt == 'Y'), f, j] += \
                        .5 * w[f, k, 1] + .5 * w[f, k, 3]
        y = np.rollaxis(y, 1, 3)
        return y

    @staticmethod
    def gpu_conv_seq(x, w, b):
        y = convolve_sequence(x, w, b)
        return y

    def conv_seq_test_setup(self, height, width, filter_width, n_filters):
        for dtype, err_tol in ((np.float32, self.FLOAT_ERR_TOL),):
                               # (np.float64, self.DOUBLE_ERR_TOL)):
            seq = sample_sequence(width, height)
            x = gpuarray.to_gpu(encode_sequence(seq))
            w = gpuarray.to_gpu(np.random.rand(n_filters, filter_width, 4).astype(dtype))
            b = gpuarray.to_gpu(np.random.rand(n_filters).astype(dtype))
            y_np = self.cpu_conv_seq(x.get(), w.get(), b.get())
            y = self.gpu_conv_seq(x, w, b)
            y_cpu = y.get()

            self.assertLess(np.max(np.abs((y_cpu - y_np) / y_cpu)), err_tol)
            del x, w, b, y

    def test_conv_seq_matrix_small(self):
        for _ in range(20):
            n = np.random.randint(10, 100)
            m = np.random.randint(100, 200)
            n_filters = np.random.randint(2, 50)
            filter_width = np.random.choice([4, 8, 16, 32])

            self.conv_seq_test_setup(n, m, filter_width, n_filters)

    def test_conv_seq_matrix_big_height(self):
        for _ in range(20):
            n = np.random.randint(200, 1000)
            m = np.random.randint(4, 9)
            n_filters = np.random.randint(2, 5)
            self.conv_seq_test_setup(n, m, 4, n_filters)

    def test_conv_seq_matrix_big_width(self):
        for _ in range(20):
            n = np.random.randint(4, 9)
            m = np.random.randint(200, 1000)
            n_filters = np.random.randint(2, 5)
            self.conv_seq_test_setup(n, m, 4, n_filters)

    def test_conv_seq_matrix_big(self):
        for _ in range(20):
            n = np.random.randint(200, 1000)
            m = np.random.randint(200, 1000)
            n_filters = np.random.randint(2, 5)
            self.conv_seq_test_setup(n, m, 4, n_filters)

    def test_conv_seq_matrix_big_filter(self):
        for _ in range(20):
            n = np.random.randint(200, 1000)
            m = np.random.randint(200, 1000)
            w = 2 * np.random.randint(2, 5)
            n_filters = np.random.randint(2, 5)
            self.conv_seq_test_setup(n, m, w, n_filters)

    def test_fixed_size(self):
        for _ in range(20):
            n = 100
            m = 200
            w = 12
            n_filters = 8
            self.conv_seq_test_setup(n, m, w, n_filters)


class TestSeqConvolutionGradWeights(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-3
    DOUBLE_ERR_TOL = 1e-12

    @staticmethod
    def grad_weights_cpu(input_data, df_output, n_filters, filter_width):
        height, width = input_data.shape
        output_width = width - filter_width + 1
        df_w = np.zeros((n_filters, filter_width, 4))

        for n in range(n_filters):
            for i in range(filter_width):
                df_w[n, i, 0] += df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'A')].sum() + \
                    .5 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'R')].sum() + \
                    .25 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'N')].sum()

                df_w[n, i, 1] += df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'C')].sum() + \
                    .5 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'Y')].sum() + \
                    .25 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'N')].sum()

                df_w[n, i, 2] += df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'G')].sum() + \
                    .5 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'R')].sum() + \
                    .25 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'N')].sum()

                df_w[n, i, 3] += df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'T')].sum() + \
                    .5 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'Y')].sum() + \
                    .25 * df_output[:, :, n][
                    np.bool_(input_data[:, i:i+output_width] == 'N')].sum()

        return df_w

    def grad_weights_test(self, height, width, n_filters, filter_width):
        for dtype, err_tol in (# (np.float64, self.DOUBLE_ERR_TOL),
                               (np.float32, self.FLOAT_ERR_TOL),):

            output_width = width - filter_width + 1
            eps = np.finfo(dtype).eps
            x = gpuarray.to_gpu(encode_sequence(sample_sequence(width, height)))
            df_output = gpuarray.to_gpu(
                np.random.rand(height, output_width, n_filters).astype(dtype))

            df_w = convolve_sequence_gradient(x, df_output, filter_width,
                                              n_filters)
            df_w_cpu = df_w.get()
            df_w_np = self.grad_weights_cpu(x.get(),
                                            df_output.get(),
                                            n_filters,
                                            filter_width)

            self.assertLess(np.abs((df_w_cpu - df_w_np) /
                                   (df_w_cpu + eps)).max(), err_tol)

    def test_grad_weights(self):
        for _ in range(20):
            n = np.random.randint(20, 200)
            filter_width = np.random.randint(8, 32)
            m = np.random.randint(filter_width, 200)
            n_filters = np.random.randint(2, 12)
            self.grad_weights_test(n, m, n_filters, filter_width)

    def test_grad_weights_small(self):
        for _ in range(20):
            n = np.random.randint(20, 100)
            filter_width = np.random.randint(4, 16)
            m = np.random.randint(filter_width, 32)
            n_filters = np.random.randint(2, 12)
            self.grad_weights_test(n, m, n_filters, filter_width)


class TestConvolution1D(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-5

    @staticmethod
    def convolve_1d_cpu(input, filters, bias):
        n_filters_out, filter_width, n_filters_in = filters.shape
        height, width = input.shape[:2]
        halo_width = filter_width - 1
        output_width = width - halo_width
        target = np.empty((height, output_width, n_filters_out))
        input = input.reshape((input.shape[0], input.shape[1] * input.shape[2]))

        for p in range(output_width):
            for f in range(n_filters_out):
                target[:, p, f] = np.dot(input[:, n_filters_in * p : n_filters_in * (p + filter_width)],
                                         filters[f].reshape((filter_width * n_filters_in,))) + bias[f]
        return target

    def test_convolve_1d(self):
        for _ in range(20):
            height = np.random.randint(100, 500)
            n_filters_in = np.random.randint(4, 48)
            n_filters_out = np.random.randint(4, 48)
            filter_width = np.random.randint(8, 32)
            input_width = np.random.randint(100, 300)

            halo_width = filter_width - 1
            output_width = input_width - halo_width

            input_data = gpuarray.to_gpu(np.random.rand(
                height, input_width, n_filters_in).astype(np.float32))
            filters = gpuarray.to_gpu(
                np.random.rand(n_filters_out, filter_width, n_filters_in)
                .astype(np.float32))
            bias = gpuarray.to_gpu(
                np.random.rand(n_filters_out).astype(np.float32))

            target_gpu = convolve_1d(input_data, filters, bias)
            target_cpu = self.convolve_1d_cpu(input_data.get(), filters.get(), bias.get())

            self.assertLess(np.max(np.abs(target_cpu - target_gpu.get()) / target_cpu),
                            self.FLOAT_ERR_TOL)
            del input_data, target_gpu, filters, bias


class TestConvolution1DGradientFilters(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-4

    @staticmethod
    def conv_1d_grad_filter_cpu(input_data, df_output, filter_width):
        height, input_width, n_filters_in = input_data.shape
        output_width, n_filters_out = df_output.shape[1:]

        df_filters = np.zeros((n_filters_out, filter_width, n_filters_in), np.float32)

        for p in range(filter_width):
            for c in range(n_filters_in):
                df_filters[:, p, c] = np.sum(
                    df_output *
                    input_data[:, p:p+output_width, c][:, :, np.newaxis],
                    (0, 1))

        return df_filters

    def test_convolve_1d_grad_filters(self):
        for _ in range(20):
            height = np.random.randint(100, 500)
            n_filters_in = np.random.randint(1, 48)
            n_filters_out = np.random.randint(1, 48)
            filter_width = np.random.randint(1, 48)
            input_width = np.random.randint(100, 300)

            halo_width = filter_width - 1
            output_width = input_width - halo_width

            input_data = gpuarray.to_gpu(np.random.rand(
                height, input_width, n_filters_in).astype(np.float32))
            df_output = gpuarray.to_gpu(np.random.rand(
                height, output_width, n_filters_out).astype(np.float32))

            df_filters_gpu = convolve_1d_gradient_filters(
                input_data, df_output, filter_width)
            df_filters_cpu = self.conv_1d_grad_filter_cpu(
                input_data.get(), df_output.get(), filter_width)

            rel_err = np.max(np.abs(df_filters_cpu - df_filters_gpu.get()) / df_filters_cpu)
            # if not rel_err < self.FLOAT_ERR_TOL: import pudb; pudb.set_trace()
            self.assertLess(rel_err, self.FLOAT_ERR_TOL)
            del input_data, df_output, df_filters_gpu
    

class TestConvolution1DGradientInput(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-4

    @staticmethod
    def conv_1d_grad_input_cpu(filters, df_output):
        n_filters_out, filter_width, n_filters_in = filters.shape
        height, output_width = df_output.shape[:2]

        halo_width = filter_width - 1
        input_width = output_width + halo_width

        df_input = np.zeros((height, input_width, n_filters_in), np.float32)

        for p in range(filter_width):
            for c in range(n_filters_in):
                df_input[:, p:p+output_width, c] += np.sum(
                    df_output * filters[:, p, c][np.newaxis, np.newaxis, :], 2)

        return df_input

    def test_convolve_1d_grad_input(self):
        for _ in range(20):
            height = np.random.randint(100, 500)
            n_filters_in = np.random.randint(1, 48)
            n_filters_out = np.random.randint(1, 48)
            filter_width = np.random.randint(1, 48)
            input_width = np.random.randint(100, 300)

            halo_width = filter_width - 1
            output_width = input_width - halo_width

            filters = gpuarray.to_gpu(np.random.rand(
                n_filters_out, filter_width, n_filters_in).astype(np.float32))
            df_output = gpuarray.to_gpu(np.random.rand(
                height, output_width, n_filters_out).astype(np.float32))

            df_input_gpu = convolve_1d_gradient_input(
                df_output, filters)
            df_input_cpu = self.conv_1d_grad_input_cpu(
                filters.get(), df_output.get())

            rel_err = np.max(np.abs(df_input_cpu - df_input_gpu.get()) / df_input_cpu)
            # if not rel_err < self.FLOAT_ERR_TOL: import pudb; pudb.set_trace()
            self.assertLess(rel_err, self.FLOAT_ERR_TOL)
            del filters, df_output, df_input_gpu


class TestMaxPool(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-20
    DOUBLE_ERR_TOL = 1e-20

    @staticmethod
    def max_pool_cpu(x, pooling_size):
        height, input_width, n_filters = x.shape
        output_width = input_width // pooling_size
        y = x.reshape((height, output_width, pooling_size, n_filters))\
             .max(2)
        return y

    def max_pool_test(self, height, width, pool_size, n_filters):
        for dtype, err_tol in ((np.float32, self.FLOAT_ERR_TOL),):
                               # (np.float64, self.DOUBLE_ERR_TOL)):

            mat = gpuarray.to_gpu(np.random.rand(height, width, n_filters)
                                  .astype(dtype))
            target, argmax = max_pool(mat, pool_size)
            target_cpu = target.get()
            target_np = self.max_pool_cpu(mat.get(), pool_size)
            self.assertLess(np.abs(
                (target_cpu - target_np) / target_cpu).max(),
                err_tol)
            del mat, target, argmax

    def test_max_pool(self):
        for _ in range(20):
            height = np.random.randint(100, 1000)
            pool_size = np.random.randint(2, 64)
            width = pool_size * np.random.randint(20, 300)
            n_filters = np.random.randint(2, 64)
            self.max_pool_test(height, width, pool_size, n_filters)


class TestSumPool(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-20
    DOUBLE_ERR_TOL = 1e-20

    @staticmethod
    def sum_pool_cpu(x, pooling_size):
        height, input_width, n_filters = x.shape
        output_width = input_width // pooling_size
        y = x.reshape((height, output_width, pooling_size, n_filters))\
             .sum(2)
        return y

    def sum_pool_test(self, height, width, pool_size, n_filters):
        for dtype, err_tol in ((np.float32, self.FLOAT_ERR_TOL),):
                               # (np.float64, self.DOUBLE_ERR_TOL)):

            mat = gpuarray.to_gpu(np.random.rand(height, width, n_filters)
                                  .astype(dtype))
            target = sum_pool(mat, pool_size)
            target_cpu = target.get()
            target_np = self.sum_pool_cpu(mat.get(), pool_size)
            self.assertLess(np.abs(
                (target_cpu - target_np) / target_cpu).max(),
                err_tol)
            del mat, target

    def test_sum_pool(self):
        for _ in range(20):
            height = np.random.randint(100, 1000)
            pool_size = np.random.randint(2, 64)
            width = pool_size * np.random.randint(20, 300)
            n_filters = np.random.randint(2, 64)
            self.sum_pool_test(height, width, pool_size, n_filters)


class TestMaxPoolGradient(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-20
    DOUBLE_ERR_TOL = 1e-20

    @staticmethod
    def max_pool_grad_cpu(mat, mat_pooled, argmax,
                          df_output, pool_size):
        height, width, n_filters = mat.shape
        width_pooled = mat_pooled.shape[1]

        df_input = np.zeros_like(mat).reshape((height, width_pooled, pool_size, n_filters))
        idx = np.c_[list(product(range(height), range(width_pooled), range(n_filters))), argmax.ravel()]
        idx = np.c_[idx[:, :2], idx[:, 3], idx[:, 2]]
        df_input[zip(*idx)] = df_output.ravel()

        return df_input.reshape(mat.shape)

    def max_pool_grad_test(self, height, width, pool_size, n_filters):
        for dtype in (np.float32, ): # np.float64):
            mat = gpuarray.to_gpu(np.random.rand(height, width, n_filters).astype(dtype))
            mat_pooled, argmax = max_pool(mat, pool_size)
            df_output = gpuarray.to_gpu(np.random.rand(*mat_pooled.shape).astype(dtype))
            df_input = max_pool_gradient(mat, argmax, df_output)
            df_input_cpu = df_input.get()
            df_input_np = self.max_pool_grad_cpu(mat.get(), mat_pooled.get(),
                                                 argmax.get(),
                                                 df_output.get(), pool_size)
            self.assertTrue(np.all(df_input_cpu == df_input_np))
            del mat, mat_pooled, df_output, df_input, argmax

    def test_max_pool_grad(self):
        for _ in range(20):
            n = np.random.randint(10, 200)
            pool_size = np.random.randint(64, 256)
            m = np.random.randint(10, 20) * pool_size
            n_filters = np.random.randint(64, 512)
            self.max_pool_grad_test(n, m, pool_size, n_filters)


class TestSumPoolGradient(unittest.TestCase):
    FLOAT_ERR_TOL = 1e-20
    DOUBLE_ERR_TOL = 1e-20

    @staticmethod
    def sum_pool_grad_cpu(mat, df_output, pool_size):
        height, width, n_filters = mat.shape
        width_pooled = width // pool_size

        df_input = np.zeros_like(mat).reshape((height, width_pooled, pool_size, n_filters))
        df_input[:] = df_output.reshape((height, width_pooled, n_filters))[:, :, np.newaxis, :]

        return df_input.reshape(mat.shape)

    def sum_pool_grad_test(self, height, width, pool_size, n_filters):
        for dtype in (np.float32, ): # np.float64):
            width_pooled = width // pool_size
            mat = gpuarray.to_gpu(np.random.rand(height, width, n_filters).astype(dtype))
            df_output = gpuarray.to_gpu(np.random.rand(height, width_pooled, n_filters).astype(dtype))
            df_input = sum_pool_gradient(mat, df_output)
            df_input_cpu = df_input.get()
            df_input_np = self.sum_pool_grad_cpu(mat.get(), df_output.get(),
                                                 pool_size)
            self.assertTrue(np.all(df_input_cpu == df_input_np))
            del mat, df_output, df_input

    def test_sum_pool_grad(self):
        for _ in range(20):
            n = np.random.randint(10, 200)
            pool_size = np.random.randint(64, 256)
            m = np.random.randint(10, 20) * pool_size
            n_filters = np.random.randint(64, 512)
            self.sum_pool_grad_test(n, m, pool_size, n_filters)


class TestConvNet(unittest.TestCase):
    @unittest.skip("Not implemented")
    def test_conv_net(self):
        seq = ['A' + ''.join([random.choice('ACGT') for _ in range(7)])
               for _ in range(100)] + \
              ['T' + ''.join([random.choice('ACGT') for _ in range(7)])
               for _ in range(100)]
        targets = np.array(100 * [[1., 0.]] +
                           100 * [[0., 1.]], dtype=np.float32)

        shuffle_idx = np.random.permutation(len(seq))
        seq = [seq[i] for i in shuffle_idx]
        targets = gpuarray.to_gpu(targets[shuffle_idx])

        test_error = 1
        train_data = SeqArrayDataProvider(seq, targets, 10)

        for _ in range(10):
            model = SequenceConvolutionNet(
                train_data.enc_seq.shape[1], 2, 32, 5, 8, [],
                activation_function='tanh')

            optimizer = SGD(model, SimpleSGDUpdate, train_data, train_data,
                            learning_rate_schedule=constant_scheduler(1.),
                            progress_monitor=SimpleProgressMonitor())

            optimizer.run(20)
            test_error = np.min([optimizer.best_validation_loss, test_error])

        self.assertEqual(test_error, 0.)


class TestConvolutionGradient(unittest.TestCase):
    EPSILON = 1e-2
    TOL = 1e-3

    @unittest.skip("Not implemented")
    def test_convolution_gradient(self):
        for _ in range(20):
            n_in = 36
            filter_width = 12
            n_filters = 4
            conv_layer = SequenceConvolutionLayer(
                n_in, filter_width, n_filters,
                dtype=np.float64)

            seq = [''.join((random.choice('ACGT') for i in range(n_in)))
                   for _ in range(100)]
            x = gpuarray.to_gpu(encode_sequence(seq))
            loss = checkgrad_model(conv_layer,
                                   x, epsilon=self.EPSILON)
            self.assertLess(loss, self.TOL)


class TestMultiSequenceConvolutionLayer(unittest.TestCase):
    """ Test whether what MultiSequenceConvolutionLayer is doing is identical
    to SequenceConvolutionLayer
    """

    N = 100
    multi_conv_config = [{'n_in': 50, 'n_filters': 10, 'filter_width': 5,
              'activation_function': 'tanh', 'pool_size': 5},
             {'n_in': 50, 'weight_share': 0, 'pool_size': 2},
             {'n_in': 100, 'n_filters': 12, 'filter_width': 10,
              'activation_function': 'tanh', 'pool_size': 8}]

    def setUp(self):
        seq = [sample_sequence(conf['n_in'], self.N) for conf in
              self.multi_conv_config]
        self.input = [gpuarray.to_gpu(encode_sequence(s)) for s in seq]

        # Create multi-convolution layer
        self.conv_layer_multi = MultiSequenceConvolutionLayer(
            self.multi_conv_config)

        # Convert configuration to single convolution
        single_conv_config = deepcopy(self.multi_conv_config)
        single_conv_config[1]['n_filters'] = single_conv_config[0]['n_filters']
        single_conv_config[1]['filter_width'] = \
            single_conv_config[0]['filter_width']
        single_conv_config[1]['activation_function'] = \
          single_conv_config[0]['activation_function']
        self.single_conv_config = single_conv_config

        # Create single convolution layers
        self.conv_layers_single = [SequenceConvolutionLayer(
            conf['n_in'], conf['filter_width'],
            conf['n_filters'],
            conf['activation_function'])
            for conf in single_conv_config]

        # Weight-sharing
        self.conv_layers_single[0].parameters = \
          (self.conv_layer_multi.W[0], self.conv_layer_multi.b[0])
        self.conv_layers_single[1].parameters = \
          (self.conv_layer_multi.W[0], self.conv_layer_multi.b[0])
        self.conv_layers_single[2].parameters = \
          (self.conv_layer_multi.W[1], self.conv_layer_multi.b[1])

        self.maxpool_layers_single = \
            [MaxPoolingLayer(conf['n_in'], conf['pool_size'],
                             conf['n_filters'])
             for conf in self.single_conv_config]

    @unittest.skip("Not implemented")
    def test_feed_forward(self):
        activations_multi, argmax, filtermaps, dropout_mask, activations_fc = \
          self.conv_layer_multi.feed_forward(self.input)

        filtermaps_single = []
        argmax_single = []
        activations_single = []
        for layer_conv, layer_pool, input_single \
          in izip(self.conv_layers_single,
                  self.maxpool_layers_single, self.input):

            filtermap, = layer_conv.feed_forward(input_single)
            filtermaps_single.append(filtermap)
            activations, argmax = layer_pool.feed_forward(filtermap)
            argmax_single.append(argmax)
            activations_single.append(activations)

        activations_joined = np.concatenate(
            [a.get() for a in activations_single], 1)

        self.assertEqual(
            np.abs(activations_multi.get() - activations_joined).max(), 0.)

    @unittest.skip("Not implemented")
    def test_backprop(self):
        activations_multi, argmax_multi, filtermaps_multi, \
        dropout_mask, activations_fc = \
          self.conv_layer_multi.feed_forward(self.input)

        df_output_cpu = [np.asarray(np.random.rand(self.N, l.n_units),
                                    dtype=activations_multi.dtype)
                         for l in self.maxpool_layers_single]
        df_output_single = map(gpuarray.to_gpu, df_output_cpu)
        df_output_multi = gpuarray.to_gpu(
            np.ascontiguousarray(np.concatenate(df_output_cpu, 1)))

        grads_multi_conv, df_filtermaps_multi = \
          self.conv_layer_multi.backprop(
              self.input, df_output_multi,
              cache=(activations_multi, argmax_multi,
                     filtermaps_multi, dropout_mask, activations_fc))

        filtermaps_single = []
        argmax_single = []
        activations_single = []
        df_W_single = []
        df_b_single = []
        for i, (layer_conv, layer_pool, input_single, df_o) \
          in enumerate(izip(self.conv_layers_single,
                            self.maxpool_layers_single,
                            self.input, df_output_single)):
            filtermap, = layer_conv.feed_forward(input_single)
            filtermaps_single.append(filtermap)

            activations, argmax = layer_pool.feed_forward(filtermap)
            argmax_single.append(argmax)
            activations_single.append(activations)

            _, df_filtermap = layer_pool.backprop(filtermap, df_o,
                                                  cache=(activations, argmax))
            (df_W_layer, df_b_layer), _ = layer_conv.backprop(
                input_single, df_filtermap, (filtermap,))

            if i in (0, 2):
                df_W_single.append(df_W_layer)
                df_b_single.append(df_b_layer)
            elif i == 1:
                df_W_single[0] += df_W_layer
                df_b_single[0] += df_b_layer

        grads_single = df_W_single + df_b_single

        for g_multi, g_single in izip(grads_multi_conv, grads_single):
            if g_multi is None and g_single is None: continue
            self.assertEqual(np.abs(g_multi.get() - g_single.get()).max(), 0.)


if __name__ == '__main__':
    unittest.main()
