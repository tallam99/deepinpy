"""Recon object for combining system blocks (such as datasets and transformers),
model blocks (such as CNNs and ResNets), and optimization blocks (such as conjugate
gradient descent)."""

#!/usr/bin/env python

import numpy as np
import torch
import sys

import pytorch_lightning as pl

from deepinpy.utils import utils
from deepinpy import opt
import deepinpy.utils.complex as cp
from deepinpy.forwards import MultiChannelMRIDataset

from torchvision.utils import make_grid


class Recon(pl.LightningModule):
    """An abstract class for implementing system-model-optimization (SMO) constructions.

    The Recon is an abstract class which outlines common functionality for all SMO structure implementations. All of them share hyperparameter initialization, MCMRI dataset processing and loading, loss function, training step, and optimizer code. Each implementation of Recon must provide batch, forward, and get_metadata methods in order to define how batches are created from the data, how the model performs its forward pass, and what metadata the user should be able to return. Currently, Recon automatically builds the dataset as an MultiChannelMRIDataset object; overload _build_data to circumvent this.

    Args:
        hprams (dict): Key-value pairings with parameter names as keys.

    Attributes:
        hprams (dict): Key-value pairings with hyperparameter names as keys.
        _loss_fun (func): Set to use either torch.nn.MSELoss or _abs_loss_fun.
        D (MultiChannelMRIDataset): Holds the MCMRI dataset.

    """

    def __init__(self, hparams):
        super(Recon, self).__init__()

        self._init_hparams(hparams)
        self._build_data()

    def _init_hparams(self, hparams):
        self.hparams = hparams

        self._loss_fun = torch.nn.MSELoss(reduction='sum')

        if hparams.abs_loss:
            self.loss_fun = self._abs_loss_fun
        else:
            self.loss_fun = self._loss_fun


    def _build_data(self):
        self.D = MultiChannelMRIDataset(data_file=self.hparams.data_file, stdev=self.hparams.stdev, num_data_sets=self.hparams.num_data_sets, adjoint=False, id=0, clear_cache=False, cache_data=False, scale_data=False, fully_sampled=self.hparams.fully_sampled, data_idx=None, inverse_crime=self.hparams.inverse_crime, noncart=self.hparams.noncart)

    def _abs_loss_fun(self, x_hat, imgs):
        x_hat_abs = torch.sqrt(x_hat.pow(2).sum(dim=-1))
        imgs_abs = torch.sqrt(imgs.pow(2).sum(dim=-1))
        return self._loss_fun(x_hat_abs, imgs_abs)

    def batch(self, data):
        """Not implemented, should define a forward operator A and the adjoint matrix of the input x.

        Args:
            data (Tensor): The data which the batch will be drawn from.

        Raises:
        	NotImplementedError: Method needs to be implemented.
        """

        raise NotImplementedError

    def forward(self, y):
        """Not implemented, should perform a prediction using the implemented model.

        Args:
        	y (Tensor): The data which will be passed to the model for processing.

        Returns:
            The model’s prediction in Tensor form.

        Raises:
        	NotImplementedError: Method needs to be implemented.
        """

    def get_metadata(self):
        """Accesses metadata for the Recon.

        Returns:
            A dict holding the Recon’s metadata.

        Raises:
        	NotImplementedError: Method needs to be implemented.
        """
        raise NotImplementedError

    # FIXME: batch_nb parameter appears unused.
    def training_step(self, batch, batch_nb):
        """Defines a training step solving deep inverse problems, including batching, performing a forward pass through
        the model, and logging data. This may either be supervised or unsupervised based on hyperparameters.

        Args:
            batch (tuple): Should hold the indices of data and the corresponding data, in said order.
            batch_nb (None): Currently unimplemented.

        Returns:
            A dict holding performance data and current epoch for performance tracking over time.
        """

        idx, data = batch
        idx = utils.itemize(idx)
        imgs = data['imgs']
        inp = data['out']

        self.batch(data)

        x_hat = self.forward(inp)

        try:
            num_cg = self.get_metadata()['num_cg']
        except KeyError:
            num_cg = 0

        _b = inp.shape[0]
        if _b == 1 and idx == 0:
                _idx = 0
        elif _b > 1 and 0 in idx:
            _idx = idx.index(0)
        else:
            _idx = None
        if _idx is not None:
            with torch.no_grad():
                if self.x_adj is None:
                    x_adj = self.A.adjoint(inp)
                else:
                    x_adj = self.x_adj
                _x_hat = utils.t2n(x_hat[_idx,...])
                _x_gt = utils.t2n(imgs[_idx,...])
                _x_adj = utils.t2n(x_adj[_idx,...])

                myim = torch.tensor(np.stack((np.abs(_x_hat), np.angle(_x_hat)), axis=0))[:, None, ...]
                grid = make_grid(myim, scale_each=True, normalize=True, nrow=8, pad_value=10)
                if self.logger:
                    self.logger.experiment.add_image('2_train_prediction', grid, self.current_epoch)

                if self.current_epoch == 0:
                    myim = torch.tensor(np.stack((np.abs(_x_gt), np.angle(_x_gt)), axis=0))[:, None, ...]
                    grid = make_grid(myim, scale_each=True, normalize=True, nrow=8, pad_value=10)
                    if self.logger:
                        self.logger.experiment.add_image('1_ground_truth', grid, 0)

                    myim = torch.tensor(np.stack((np.abs(_x_adj), np.angle(_x_adj)), axis=0))[:, None, ...]
                    grid = make_grid(myim, scale_each=True, normalize=True, nrow=8, pad_value=10)
                    if self.logger:
                        self.logger.experiment.add_image('0_input', grid, 0)


        if self.hparams.self_supervised:
            pred = self.A.forward(x_hat)
            gt = inp
        else:
            pred = x_hat
            gt = imgs

        loss = self.loss_fun(pred, gt)

        _loss = loss.clone().detach().requires_grad_(False)
        try:
            _lambda = self.l2lam.clone().detach().requires_grad_(False)
        except:
            _lambda = 0
        _epoch = self.current_epoch
        _nrmse = (opt.ip_batch(x_hat - imgs) / opt.ip_batch(imgs)).sqrt().mean().detach().requires_grad_(False)
        _num_cg = np.max(num_cg)

        log_dict = {
                'lambda': _lambda,
                'train_loss': _loss,
                'epoch': self.current_epoch,
                'nrmse': _nrmse, 
                'max_num_cg': _num_cg,
                'val_loss': 0.,
                }
        return {
                'loss': loss,
                'log': log_dict,
                'progress_bar': log_dict,
                }


    def configure_optimizers(self):
        """Determines whether to use Adam or SGD depending on hyperparameters.

        Returns:
            Torch’s implementation of SGD or Adam, depending on hyperparameters.
        """

        if 'adam' in self.hparams.solver:
            return [torch.optim.Adam(self.parameters(), lr=self.hparams.step)]
        elif 'sgd' in self.hparams.solver:
            return [torch.optim.SGD(self.parameters(), lr=self.hparams.step)]

    @pl.data_loader
    def train_dataloader(self):
        """Creates a DataLoader object, with distributed training if specified in the hyperparameters.

        Returns:
            A PyTorch DataLoader that has been configured according to the hyperparameters.
        """

        if self.hparams.distributed_training:
            sampler = torch.utils.data.distributed.DistributedSampler(self.D, shuffle=self.hparams.shuffle)
            shuffle = False
        else:
            sampler = None
            shuffle = self.hparams.shuffle
        return torch.utils.data.DataLoader(self.D, batch_size=self.hparams.batch_size, shuffle=shuffle, num_workers=0, drop_last=True, sampler=sampler)
