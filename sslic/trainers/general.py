import torch
from torch import nn
from torch.utils.data import DataLoader
import os
from abc import ABC

from typing import Tuple

from .. import pkbar
from ..logger import Logger, EmptyLogger
from ..utils import AllReduce, after_init_world_size_n_rank
from ssl_eval import Evaluator


class GeneralTrainer(ABC):
    """GeneralTrainer
    A trainer class responsible for training N epochs.
    This is an abstract class, both SSL and LinearEval methods will
    be inherited from this class.

    Parameters
    ----------
    model : nn.Module
        The model we desire to train.
    optimizer : torch.optim.Optimizer
        The optimizer to use for training.
    data_loaders : Tuple[DataLoader, DataLoader]
        Train and validation data loaders
    save_params : dict, optional
        Parameters used for saving the network.
        These parameters can be the name of the method and the name
        of the dataset which together define the model architecture
        the code built up. It must also contain a save_dir key
        which tells to which folder the model should be saved. If
        save_dir is None, the trainer will not save anything.
        By default {"save_dir": None}
    evaluator : KNNEvaluator, optional
        An evaluator for the model which calculates the KNN accuracies.
        If None, this step is skipped.
        By default None.
    """

    def __init__(self,
                 model: nn.Module,
                 optimizer: torch.optim.Optimizer,
                 data_loaders: Tuple[DataLoader, DataLoader],
                 save_params: dict = {"save_dir": None},
                 evaluator: Evaluator = None,
                 logger: Logger = EmptyLogger()):
        self.model = model
        self.optimizer = optimizer
        self.train_loader, self.val_loader = data_loaders
        self.save_dir = save_params.pop('save_dir')
        self.save_dict = save_params
        self.evaluator = evaluator
        self.scaler = torch.cuda.amp.GradScaler()
        self.world_size, self.rank = after_init_world_size_n_rank()
        if not (self.rank is None or self.rank == 0):
            self.logger = EmptyLogger()
        else:
            self.logger = logger
        self.start_epoch = 0

        # Progress bar with running average metrics
        self.pbar = ProgressBar([self.train_loader], self.rank)

        # Checkpoints in which we save the model
        self.save_checkpoints = []

    @property
    def device(self):
        return next(self.model.parameters()).device

    def _need_save(self, epoch: int) -> bool:
        """_need_save
        Determines if the model should be saved in this
        particular epoch.
        """
        save_dir_given = self.save_dir is not None
        in_saving_epoch = (epoch + 1) in self.save_checkpoints
        is_saving_core = self.rank is None or self.rank == 0
        return save_dir_given and in_saving_epoch and is_saving_core

    def _ckp_name(self, epoch: int):
        """_ckp_name
        Checkpoint filename. It has an own unique class, because
        each trainer might define different filenames.
        """
        return f'checkpoint_{epoch+1:04d}.pth.tar'

    def _save(self, epoch: int):
        """_save [summary]
        Save the current checkpoint to a file.
        """
        save_dict = {
            'epoch': epoch + 1,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'amp': self.scaler.state_dict(),
        }
        save_dict.update(self.save_dict)
        filename = self._ckp_name(epoch)
        os.makedirs(self.save_dir, exist_ok=True)
        filepath = os.path.join(self.save_dir, filename)
        torch.save(save_dict, filepath)

    def load(self, path):
        save_dict = torch.load(path, map_location=self.device)
        self.model.load_state_dict(save_dict['state_dict'])
        self.optimizer.load_state_dict(save_dict['optimizer'])
        self.scaler.load_state_dict(save_dict['amp'])
        self.start_epoch = save_dict['epoch']
        torch.distributed.barrier()

    def adjust_learning_rate(self, optimizer, init_lr, epoch, epochs):
        """Decay the learning rate based on schedule"""
        import math  # TODO: Shouldn't import here
        cur_lr = init_lr * 0.5 * (1. + math.cos(math.pi * epoch / epochs))
        for param_group in optimizer.param_groups:
            if 'fix_lr' in param_group and param_group['fix_lr']:
                param_group['lr'] = init_lr
            else:
                param_group['lr'] = cur_lr
        return cur_lr

    def train(self, n_epochs: int, ref_lr: float = 0.1, n_warmup_epochs: int = 10):
        """train
        Train n epochs.

        Parameters
        ----------
        n_epochs : int
            Number of epochs to train.
        ref_lr : float, optional
            Base learning rate to cosine scheduler, by default 0.1
        n_warmup_epochs : int, optional
            Number of warmup epochs, by default 10
        """

        n_warmup_iter = len(self.train_loader) * n_warmup_epochs # TODO: n_warmup_iter is unused

        for epoch in range(self.start_epoch, n_epochs):
            # Reset progress bar to the start of the line
            self.pbar.reset(epoch, n_epochs)
            self.logger.add_scalar("stats/epoch", epoch, force=True)

            # Set epoch in sampler
            if self.world_size > 1:
                self.train_loader.sampler.set_epoch(epoch)

            # Adjust learning rate accordingly to epoch
            new_lr = self.adjust_learning_rate(self.optimizer, ref_lr, epoch, n_epochs)
            self.logger.add_scalar("stats/learning_rate", new_lr, force=True)

            # Train
            self.train_an_epoch()

            # Validate
            if (epoch + 1) % 5 == 0 or epoch == 0:
                self.run_validation()

            # Save network
            if self._need_save(epoch):
                self._save(epoch)

    def train_an_epoch(self):
        for data_batch in self.train_loader:
            metrics = self.train_step(data_batch)
            self.logger.step()
            self.pbar.update(metrics)
            for k, v in metrics.items():
                self.logger.add_scalar(f"train/{k}", v)

    def run_validation(self):
        self.evaluator.generate_embeddings()
        batch_size = 4096 // self.world_size
        init_lr = 1.6
        accuracy = self.evaluator.linear_eval(batch_size=batch_size, lr=init_lr)
        self.logger.add_scalar("test/lineval_acc", accuracy, force=True)

    def train_step(self, batch: torch.Tensor):
        raise NotImplementedError

    def val_step(self, batch: torch.Tensor):
        raise NotImplementedError

    def _accuracy(self, y_hat: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        """_accuracy 
        Accuracy of the model
        """
        pred = torch.max(y_hat.data, 1)[1]
        acc = (pred == y).sum() / len(y)
        return AllReduce.apply(acc)


class ProgressBar:

    def __init__(self, data_loaders, rank):
        self.n_iter = sum([len(x) for x in data_loaders])
        self.kbar = None
        self.is_active = rank is None or rank == 0

    def reset(self, epoch_i, n_epochs):
        if self.is_active:
            self.kbar = pkbar.Kbar(target=self.n_iter,
                                   epoch=epoch_i,
                                   num_epochs=n_epochs,
                                   width=8,
                                   always_stateful=False)

    def update(self, value_dict):
        if self.is_active:
            values = [(k, v) for (k, v) in value_dict.items()]
            self.kbar.add(1, values=values)
