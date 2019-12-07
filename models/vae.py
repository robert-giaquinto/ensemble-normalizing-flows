import numpy as np
import torch
import torch.nn as nn
import torch.distributions as D

import models.flows as flows
from models.layers import GatedConv2d, GatedConvTranspose2d
from utils.distributions import log_normal_standard, log_normal_diag
import random
from scipy.optimize import line_search
from utils.utilities import safe_log

from optimization.loss import calculate_loss


class VAE(nn.Module):
    """
    Base class for VAE's with normalizing flows.
    """
    def __init__(self, args):
        super(VAE, self).__init__()

        # extract model settings from args
        self.z_size = args.z_size
        self.input_type = args.input_type

        if args.input_size == [1, 28, 20]:
            self.last_kernel_size = (7, 5)
            self.last_pad = 2
        elif args.input_size == [3, 32, 32]:
            self.last_kernel_size = 7
            self.last_pad = 1
        else:
            self.last_kernel_size = 7
            self.last_pad = 2
        
        self.simple = True
        if self.simple:
            self.input_size = np.prod(args.input_size)
        else:
            self.input_size = args.input_size[0]
            
        self.q_z_nn_output_dim = 256
        self.q_z_nn_hidden_dim = 128

        if not args.density_evaluation:
            self.q_z_nn, self.q_z_mean, self.q_z_var = self.create_encoder()
            self.p_x_nn, self.p_x_mean = self.create_decoder()

        # auxiliary
        if args.cuda:
            self.FloatTensor = torch.cuda.FloatTensor
        else:
            self.FloatTensor = torch.FloatTensor

        # log-det-jacobian = 0 without flows
        self.log_det_j = self.FloatTensor(1).zero_()

        # base distribution for calculation of log prob under the model
        self.register_buffer('base_dist_mean', torch.randn(self.z_size, device=args.device).normal_(0, 0.1))
        #self.register_buffer('base_dist_mean', torch.ones(self.z_size, device=args.device))
        self.register_buffer('base_dist_var', 3.0 * torch.ones(self.z_size, device=args.device))

    @property
    def base_dist(self):
        #rval = D.MultivariateNormal(self.base_dist_mean, self.base_dist_var)
        rval = D.Normal(self.base_dist_mean, self.base_dist_var)
        return rval
    
    def create_encoder(self):
        """
        Helper function to create the elemental blocks for the encoder. Creates a gated convnet encoder.
        the encoder expects data as input of shape (batch_size, num_channels, width, height).
        """
        if self.input_type == 'binary':
            if self.simple:
                q_z_nn = nn.Sequential(
                    nn.Linear(self.input_size, self.q_z_nn_hidden_dim),
                    nn.ReLU(),
                    nn.Linear(self.q_z_nn_hidden_dim, self.q_z_nn_output_dim),
                    nn.Softplus(),
                )
                q_z_mean = nn.Linear(self.q_z_nn_output_dim, self.z_size)
                q_z_var = nn.Sequential(
                    nn.Linear(self.q_z_nn_output_dim, self.z_size),
                    nn.Softplus(),
                )
            else:
                q_z_nn = nn.Sequential(
                    GatedConv2d(self.input_size, 32, 5, 1, 2),
                    GatedConv2d(32, 32, 5, 2, 2),
                    GatedConv2d(32, 64, 5, 1, 2),
                    GatedConv2d(64, 64, 5, 2, self.last_pad),
                    GatedConv2d(64, 64, 5, 1, 2),
                    GatedConv2d(64, self.q_z_nn_output_dim, self.last_kernel_size, 1, 0),
                )
                q_z_mean = nn.Linear(self.q_z_nn_output_dim, self.z_size)
                q_z_var = nn.Sequential(
                    nn.Linear(self.q_z_nn_output_dim, self.z_size),
                    nn.Softplus(),
                )
            return q_z_nn, q_z_mean, q_z_var

        elif self.input_type == 'multinomial':
            act = None
            q_z_nn = nn.Sequential(
                GatedConv2d(self.input_size, 32, 5, 1, 2, activation=act),
                GatedConv2d(32, 32, 5, 2, 2, activation=act),
                GatedConv2d(32, 64, 5, 1, 2, activation=act),
                GatedConv2d(64, 64, 5, 2, self.last_pad, activation=act),
                GatedConv2d(64, 64, 5, 1, 2, activation=act),
                GatedConv2d(64, 256, self.last_kernel_size, 1, 0, activation=act)
            )
            q_z_mean = nn.Linear(256, self.z_size)
            q_z_var = nn.Sequential(
                nn.Linear(256, self.z_size),
                nn.Softplus(),
                nn.Hardtanh(min_val=0.01, max_val=7.)

            )
            return q_z_nn, q_z_mean, q_z_var

    def create_decoder(self):
        """
        Helper function to create the elemental blocks for the decoder. Creates a gated convnet decoder.
        """

        num_classes = 256

        if self.input_type == 'binary':
            if self.simple:
                p_x_nn = nn.Sequential(
                    nn.Linear(self.z_size, self.q_z_nn_hidden_dim),
                    nn.ReLU(),
                    nn.Linear(self.q_z_nn_hidden_dim, self.q_z_nn_output_dim),
                    nn.Softplus(),
                )
                p_x_mean = nn.Sequential(
                    nn.Linear(self.q_z_nn_output_dim, self.input_size),
                    nn.Sigmoid()
                )
            else:
                p_x_nn = nn.Sequential(
                    GatedConvTranspose2d(self.z_size, 64, self.last_kernel_size, 1, 0),
                    GatedConvTranspose2d(64, 64, 5, 1, 2),
                    GatedConvTranspose2d(64, 32, 5, 2, 2, 1),
                    GatedConvTranspose2d(32, 32, 5, 1, 2),
                    GatedConvTranspose2d(32, 32, 5, 2, 2, 1),
                    GatedConvTranspose2d(32, 32, 5, 1, 2)
                )
                p_x_mean = nn.Sequential(
                    nn.Conv2d(32, self.input_size, 1, 1, 0),
                    nn.Sigmoid()
                )
            return p_x_nn, p_x_mean

        elif self.input_type == 'multinomial':
            act = None
            p_x_nn = nn.Sequential(
                GatedConvTranspose2d(self.z_size, 64, self.last_kernel_size, 1, 0, activation=act),
                GatedConvTranspose2d(64, 64, 5, 1, 2, activation=act),
                GatedConvTranspose2d(64, 32, 5, 2, self.last_pad, 1, activation=act),
                GatedConvTranspose2d(32, 32, 5, 1, 2, activation=act),
                GatedConvTranspose2d(32, 32, 5, 2, 2, 1, activation=act),
                GatedConvTranspose2d(32, 32, 5, 1, 2, activation=act)
            )
            p_x_mean = nn.Sequential(
                nn.Conv2d(32, 256, 5, 1, 2),
                nn.Conv2d(256, self.input_size * num_classes, 1, 1, 0),
                # output shape: batch_size, num_channels * num_classes, pixel_width, pixel_height
            )

            return p_x_nn, p_x_mean

        else:
            raise ValueError('invalid input type!!')

    def reparameterize(self, mu, var):
        """
        Samples z from a multivariate Gaussian with diagonal covariance matrix using the
        reparameterization trick.
        """
        std = var.sqrt()
        eps = self.FloatTensor(std.size()).normal_()
        z = eps.mul(std).add_(mu)
        return z

    def encode(self, x):
        """
        Encoder expects following data shapes as input: shape = (batch_size, num_channels, width, height)
        """
        h = self.q_z_nn(x)
        if not self.simple:
            h = h.view(h.size(0), -1)
        mean = self.q_z_mean(h)
        var = self.q_z_var(h)
        return mean, var

    def decode(self, z):
        """
        Decoder outputs reconstructed image in the following shapes:
        x_mean.shape = (batch_size, num_channels, width, height)
        """
        if not self.simple:
            z = z.view(z.size(0), self.z_size, 1, 1)
        h = self.p_x_nn(z)
        x_mean = self.p_x_mean(h)
        return x_mean

    def forward(self, x):
        """
        Evaluates the model as a whole, encodes and decodes. Note that the log det jacobian is zero
         for a plain VAE (without flows), and z_0 = z_k.
        """
        # mean and variance of z
        z_mu, z_var = self.encode(x)
        # sample z
        z = self.reparameterize(z_mu, z_var)
        x_mean = self.decode(z)
        return x_mean, z_mu, z_var, self.log_det_j, z, z


class OrthogonalSylvesterVAE(VAE):
    """
    Variational auto-encoder with orthogonal flows in the encoder.
    """

    def __init__(self, args):
        super(OrthogonalSylvesterVAE, self).__init__(args)

        # Initialize log-det-jacobian to zero
        self.log_det_j = 0.

        # Flow parameters
        flow = flows.Sylvester
        self.num_flows = args.num_flows
        self.num_ortho_vecs = args.num_ortho_vecs

        assert (self.num_ortho_vecs <= self.z_size) and (self.num_ortho_vecs > 0)

        # Orthogonalization parameters
        if self.num_ortho_vecs == self.z_size:
            self.cond = 1.e-5
        else:
            self.cond = 1.e-6

        self.steps = 100
        identity = torch.eye(self.num_ortho_vecs, self.num_ortho_vecs)
        # Add batch dimension
        identity = identity.unsqueeze(0)
        # Put identity in buffer so that it will be moved to GPU if needed by any call of .cuda
        self.register_buffer('_eye', identity)  #self.register_buffer('_eye', Variable(identity))
        self._eye.requires_grad = False

        # Masks needed for triangular R1 and R2.
        triu_mask = torch.triu(torch.ones(self.num_ortho_vecs, self.num_ortho_vecs), diagonal=1)
        triu_mask = triu_mask.unsqueeze(0).unsqueeze(3)
        diag_idx = torch.arange(0, self.num_ortho_vecs).long()

        self.register_buffer('triu_mask', triu_mask)  #self.register_buffer('triu_mask', Variable(triu_mask))
        self.triu_mask.requires_grad = False
        self.register_buffer('diag_idx', diag_idx)

        # Amortized flow parameters
        # Diagonal elements of R1 * R2 have to satisfy -1 < R1 * R2 for flow to be invertible
        self.diag_activation = nn.Tanh()

        self.amor_d = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.num_ortho_vecs * self.num_ortho_vecs)

        self.amor_diag1 = nn.Sequential(
            nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.num_ortho_vecs),
            self.diag_activation
        )
        self.amor_diag2 = nn.Sequential(
            nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.num_ortho_vecs),
            self.diag_activation
        )

        self.amor_q = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size * self.num_ortho_vecs)
        self.amor_b = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.num_ortho_vecs)

        # Normalizing flow layers
        for k in range(self.num_flows):
            flow_k = flow(self.num_ortho_vecs)
            self.add_module('flow_' + str(k), flow_k)

    def batch_construct_orthogonal(self, q):
        """
        Batch orthogonal matrix construction.
        :param q:  q contains batches of matrices, shape : (batch_size * num_flows, z_size * num_ortho_vecs)
        :return: batches of orthogonalized matrices, shape: (batch_size * num_flows, z_size, num_ortho_vecs)
        """

        # Reshape to shape (num_flows * batch_size, z_size * num_ortho_vecs)
        q = q.view(-1, self.z_size * self.num_ortho_vecs)

        norm = torch.norm(q, p=2, dim=1, keepdim=True)
        amat = torch.div(q, norm)
        dim0 = amat.size(0)
        amat = amat.resize(dim0, self.z_size, self.num_ortho_vecs)

        max_norm = 0.

        # Iterative orthogonalization
        for s in range(self.steps):
            tmp = torch.bmm(amat.transpose(2, 1), amat)
            tmp = self._eye - tmp
            tmp = self._eye + 0.5 * tmp
            amat = torch.bmm(amat, tmp)

            # Testing for convergence
            test = torch.bmm(amat.transpose(2, 1), amat) - self._eye
            norms2 = torch.sum(torch.norm(test, p=2, dim=2) ** 2, dim=1)
            norms = torch.sqrt(norms2)
            max_norm = torch.max(norms).data[0]
            if max_norm <= self.cond:
                break

        if max_norm > self.cond:
            raise ValueError(f"WARNING: orthogonalization not complete. Final max norm = {max_norm}") 

        # Reshaping: first dimension is batch_size
        amat = amat.view(-1, self.num_flows, self.z_size, self.num_ortho_vecs)
        amat = amat.transpose(0, 1)

        return amat

    def encode(self, x):
        """
        Encoder that ouputs parameters for base distribution of z and flow parameters.
        """

        batch_size = x.size(0)

        h = self.q_z_nn(x)
        h = h.view(-1, self.q_z_nn_output_dim)
        mean_z = self.q_z_mean(h)
        var_z = self.q_z_var(h)

        # Amortized r1, r2, q, b for all flows

        full_d = self.amor_d(h)
        diag1 = self.amor_diag1(h)
        diag2 = self.amor_diag2(h)

        full_d = full_d.resize(batch_size, self.num_ortho_vecs, self.num_ortho_vecs, self.num_flows)
        diag1 = diag1.resize(batch_size, self.num_ortho_vecs, self.num_flows)
        diag2 = diag2.resize(batch_size, self.num_ortho_vecs, self.num_flows)

        r1 = full_d * self.triu_mask
        r2 = full_d.transpose(2, 1) * self.triu_mask

        r1[:, self.diag_idx, self.diag_idx, :] = diag1
        r2[:, self.diag_idx, self.diag_idx, :] = diag2

        q = self.amor_q(h)
        b = self.amor_b(h)

        # Resize flow parameters to divide over K flows
        b = b.resize(batch_size, 1, self.num_ortho_vecs, self.num_flows)

        return mean_z, var_z, r1, r2, q, b

    def forward(self, x):
        """
        Forward pass with orthogonal sylvester flows for the transformation z_0 -> z_1 -> ... -> z_k.
        Log determinant is computed as log_det_j = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ].
        """

        self.log_det_j = 0.

        z_mu, z_var, r1, r2, q, b = self.encode(x)

        # Orthogonalize all q matrices
        q_ortho = self.batch_construct_orthogonal(q)

        # Sample z_0
        z = [self.reparameterize(z_mu, z_var)]

        # Normalizing flows
        for k in range(self.num_flows):

            flow_k = getattr(self, 'flow_' + str(k))
            z_k, log_det_jacobian = flow_k(z[k], r1[:, :, :, k], r2[:, :, :, k], q_ortho[k, :, :, :], b[:, :, :, k])

            z.append(z_k)
            self.log_det_j += log_det_jacobian

        x_mean = self.decode(z[-1])

        return x_mean, z_mu, z_var, self.log_det_j, z[0], z[-1]


class HouseholderSylvesterVAE(VAE):
    """
    Variational auto-encoder with householder sylvester flows in the encoder.
    """

    def __init__(self, args):
        super(HouseholderSylvesterVAE, self).__init__(args)

        # Initialize log-det-jacobian to zero
        self.log_det_j = 0.

        # Flow parameters
        flow = flows.Sylvester
        self.num_flows = args.num_flows
        self.num_householder = args.num_householder
        assert self.num_householder > 0

        identity = torch.eye(self.z_size, self.z_size)
        # Add batch dimension
        identity = identity.unsqueeze(0)
        # Put identity in buffer so that it will be moved to GPU if needed by any call of .cuda
        self.register_buffer('_eye', identity)  #self.register_buffer('_eye', Variable(identity))
        self._eye.requires_grad = False

        # Masks needed for triangular r1 and r2.
        triu_mask = torch.triu(torch.ones(self.z_size, self.z_size), diagonal=1)
        triu_mask = triu_mask.unsqueeze(0).unsqueeze(3)
        diag_idx = torch.arange(0, self.z_size).long()

        self.register_buffer('triu_mask', triu_mask)  #self.register_buffer('triu_mask', Variable(triu_mask))
        self.triu_mask.requires_grad = False
        self.register_buffer('diag_idx', diag_idx)

        # Amortized flow parameters
        # Diagonal elements of r1 * r2 have to satisfy -1 < r1 * r2 for flow to be invertible
        self.diag_activation = nn.Tanh()

        self.amor_d = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size * self.z_size)

        self.amor_diag1 = nn.Sequential(
            nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size),
            self.diag_activation
        )
        self.amor_diag2 = nn.Sequential(
            nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size),
            self.diag_activation
        )

        self.amor_q = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size * self.num_householder)

        self.amor_b = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size)

        # Normalizing flow layers
        for k in range(self.num_flows):
            flow_k = flow(self.z_size)

            self.add_module('flow_' + str(k), flow_k)

    def batch_construct_orthogonal(self, q):
        """
        Batch orthogonal matrix construction.
        :param q:  q contains batches of matrices, shape : (batch_size, num_flows * z_size * num_householder)
        :return: batches of orthogonalized matrices, shape: (batch_size * num_flows, z_size, z_size)
        """

        # Reshape to shape (num_flows * batch_size * num_householder, z_size)
        q = q.view(-1, self.z_size)

        norm = torch.norm(q, p=2, dim=1, keepdim=True)   # ||v||_2
        v = torch.div(q, norm)  # v / ||v||_2

        # Calculate Householder Matrices
        vvT = torch.bmm(v.unsqueeze(2), v.unsqueeze(1))  # v * v_T : batch_dot( B x L x 1 * B x 1 x L ) = B x L x L

        amat = self._eye - 2 * vvT  # NOTICE: v is already normalized! so there is no need to calculate vvT/vTv

        # Reshaping: first dimension is batch_size * num_flows
        amat = amat.view(-1, self.num_householder, self.z_size, self.z_size)

        tmp = amat[:, 0]
        for k in range(1, self.num_householder):
            tmp = torch.bmm(amat[:, k], tmp)

        amat = tmp.view(-1, self.num_flows, self.z_size, self.z_size)
        amat = amat.transpose(0, 1)

        return amat

    def encode(self, x):
        """
        Encoder that ouputs parameters for base distribution of z and flow parameters.
        """

        batch_size = x.size(0)

        h = self.q_z_nn(x)
        h = h.view(-1, self.q_z_nn_output_dim)
        mean_z = self.q_z_mean(h)
        var_z = self.q_z_var(h)

        # Amortized r1, r2, q, b for all flows
        full_d = self.amor_d(h)
        diag1 = self.amor_diag1(h)
        diag2 = self.amor_diag2(h)

        full_d = full_d.resize(batch_size, self.z_size, self.z_size, self.num_flows)
        diag1 = diag1.resize(batch_size, self.z_size, self.num_flows)
        diag2 = diag2.resize(batch_size, self.z_size, self.num_flows)

        r1 = full_d * self.triu_mask
        r2 = full_d.transpose(2, 1) * self.triu_mask

        r1[:, self.diag_idx, self.diag_idx, :] = diag1
        r2[:, self.diag_idx, self.diag_idx, :] = diag2

        q = self.amor_q(h)

        b = self.amor_b(h)

        # Resize flow parameters to divide over K flows
        b = b.resize(batch_size, 1, self.z_size, self.num_flows)

        return mean_z, var_z, r1, r2, q, b

    def forward(self, x):
        """
        Forward pass with orthogonal flows for the transformation z_0 -> z_1 -> ... -> z_k.
        Log determinant is computed as log_det_j = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ].
        """

        self.log_det_j = 0.
        batch_size = x.size(0)

        z_mu, z_var, r1, r2, q, b = self.encode(x)

        # Orthogonalize all q matrices
        q_ortho = self.batch_construct_orthogonal(q)

        # Sample z_0
        z = [self.reparameterize(z_mu, z_var)]

        # Normalizing flows
        for k in range(self.num_flows):

            flow_k = getattr(self, 'flow_' + str(k))
            q_k = q_ortho[k]

            z_k, log_det_jacobian = flow_k(z[k], r1[:, :, :, k], r2[:, :, :, k], q_k, b[:, :, :, k], sum_ldj=True)

            z.append(z_k)
            self.log_det_j += log_det_jacobian

        x_mean = self.decode(z[-1])

        return x_mean, z_mu, z_var, self.log_det_j, z[0], z[-1]


class TriangularSylvesterVAE(VAE):
    """
    Variational auto-encoder with triangular Sylvester flows in the encoder. Alternates between setting
    the orthogonal matrix equal to permutation and identity matrix for each flow.
    """

    def __init__(self, args):
        super(TriangularSylvesterVAE, self).__init__(args)

        # Initialize log-det-jacobian to zero
        self.log_det_j = 0.

        # Flow parameters
        flow = flows.TriangularSylvester
        self.num_flows = args.num_flows

        # permuting indices corresponding to Q=P (permutation matrix) for every other flow
        flip_idx = torch.arange(self.z_size - 1, -1, -1).long()
        self.register_buffer('flip_idx', flip_idx)

        # Masks needed for triangular r1 and r2.
        triu_mask = torch.triu(torch.ones(self.z_size, self.z_size), diagonal=1)
        triu_mask = triu_mask.unsqueeze(0).unsqueeze(3)
        diag_idx = torch.arange(0, self.z_size).long()

        self.register_buffer('triu_mask', triu_mask)  #self.register_buffer('triu_mask', Variable(triu_mask))
        self.triu_mask.requires_grad = False
        self.register_buffer('diag_idx', diag_idx)

        # Amortized flow parameters
        # Diagonal elements of r1 * r2 have to satisfy -1 < r1 * r2 for flow to be invertible
        self.diag_activation = nn.Tanh()

        self.amor_d = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size * self.z_size)

        self.amor_diag1 = nn.Sequential(
            nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size),
            self.diag_activation
        )
        self.amor_diag2 = nn.Sequential(
            nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size),
            self.diag_activation
        )

        self.amor_b = nn.Linear(self.q_z_nn_output_dim, self.num_flows * self.z_size)

        # Normalizing flow layers
        for k in range(self.num_flows):
            flow_k = flow(self.z_size)

            self.add_module('flow_' + str(k), flow_k)

    def encode(self, x):
        """
        Encoder that ouputs parameters for base distribution of z and flow parameters.
        """

        batch_size = x.size(0)

        h = self.q_z_nn(x)
        h = h.view(-1, self.q_z_nn_output_dim)
        mean_z = self.q_z_mean(h)
        var_z = self.q_z_var(h)

        # Amortized r1, r2, b for all flows
        full_d = self.amor_d(h)
        diag1 = self.amor_diag1(h)
        diag2 = self.amor_diag2(h)

        full_d = full_d.resize(batch_size, self.z_size, self.z_size, self.num_flows)
        diag1 = diag1.resize(batch_size, self.z_size, self.num_flows)
        diag2 = diag2.resize(batch_size, self.z_size, self.num_flows)

        r1 = full_d * self.triu_mask
        r2 = full_d.transpose(2, 1) * self.triu_mask

        r1[:, self.diag_idx, self.diag_idx, :] = diag1
        r2[:, self.diag_idx, self.diag_idx, :] = diag2

        b = self.amor_b(h)

        # Resize flow parameters to divide over K flows
        b = b.resize(batch_size, 1, self.z_size, self.num_flows)

        return mean_z, var_z, r1, r2, b

    def forward(self, x):
        """
        Forward pass with orthogonal flows for the transformation z_0 -> z_1 -> ... -> z_k.
        Log determinant is computed as log_det_j = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ].
        """

        self.log_det_j = 0.

        z_mu, z_var, r1, r2, b = self.encode(x)

        # Sample z_0
        z = [self.reparameterize(z_mu, z_var)]

        # Normalizing flows
        for k in range(self.num_flows):

            flow_k = getattr(self, 'flow_' + str(k))
            if k % 2 == 1:
                # Alternate with reorderering z for triangular flow
                permute_z = self.flip_idx
            else:
                permute_z = None

            z_k, log_det_jacobian = flow_k(z[k], r1[:, :, :, k], r2[:, :, :, k], b[:, :, :, k], permute_z, sum_ldj=True)

            z.append(z_k)
            self.log_det_j += log_det_jacobian

        x_mean = self.decode(z[-1])

        return x_mean, z_mu, z_var, self.log_det_j, z[0], z[-1]


