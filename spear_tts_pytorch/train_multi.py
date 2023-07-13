# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/B2. Training (Lightning).ipynb.

# %% auto 0
__all__ = []

# %% ../nbs/B2. Training (Lightning).ipynb 2
import io
import time
import random
from pathlib import Path

from fastprogress import progress_bar, master_bar
import fastprogress
import wandb

import numpy as np
import pylab as plt

import IPython

import torch
import torch.nn as nn
from torch.utils.data.dataloader import DataLoader
from torch.profiler import record_function

# %% ../nbs/B2. Training (Lightning).ipynb 3
import lightning.pytorch as pl
import math

class TrainingTask(pl.LightningModule):
    def __init__(self, model, model_hparams=None):
        super().__init__()
        self.model = model
        self.model_hparams = model_hparams
    
    def configure_optimizers(self):
        """ Initialize AdamW optimizer"""
        all_params = set(model.parameters())
        customized_params = set()
        groups = []
        group_map = {}
        for name,m in model.named_modules():
            if hasattr(m, 'no_weight_decay') or hasattr(m, 'lr_scale'):
                customized_params |= set(m.parameters())
                m_wd = 0 if hasattr(m, 'no_weight_decay') else weight_decay
                m_lr = lr * getattr(m, 'lr_scale', 1)
                group = group_map.get((m_wd, m_lr), None)
                if not group:
                    group = {"params": [], "names": [], "weight_decay": m_wd, "lr": m_lr}
                    groups.append(group)
                    group_map[(m_wd, m_lr)] = group
                group['params'] += m.parameters()
                group['names'].append(name)
                
        other_params = all_params - customized_params
        
        param_groups = groups + [
            {"names": ["other"], "params": list(other_params), "weight_decay": weight_decay },
        ]

        optimizer = torch.optim.AdamW(lr=self.model_hparams['lr0'], betas=(0.9, 0.95),
                                      fused=True, params=param_groups)
        
        # modified from https://github.com/Lightning-AI/lightning/issues/5449#issuecomment-1501597319
        def num_steps_per_epoch() -> int:
            """Get number of steps"""
            # Accessing _data_source is flaky and might break
            dataset = self.trainer.fit_loop._data_source.dataloader()
            dataset_size = len(dataset)
            num_devices = max(1, self.trainer.num_devices)
            # math.ceil so always overestimate (underestimating throws exceptions)
            num_steps = math.ceil(dataset_size / (self.trainer.accumulate_grad_batches * num_devices))
            return num_steps
        
        total_steps = self.model_hparams['epochs'] * num_steps_per_epoch()
        self.model_hparams['pct_start'] = min(0.3, self.model_hparams['warmup_steps'] / total_steps)

        lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer,
            pct_start=self.model_hparams['pct_start'],
            max_lr=self.model_hparams['lr0'],
            steps_per_epoch=num_steps_per_epoch(),
            epochs=self.model_hparams['epochs'],
            final_div_factor=25
        )

        return [optimizer], [{'scheduler': lr_scheduler, 'interval': 'step'}]
    
    def training_step(self, train_batch, batch_idx):
        train_logits, train_loss = self.model.forward(*train_batch)

        self.log("train_loss", train_loss, sync_dist=True)
        return train_loss
    
    def validation_step(self, val_batch, batch_idx):
        val_logits, val_loss = self.model.forward(*val_batch)

        logs = dict(val_loss = val_loss)
        if hasattr(self.model, 'val_true'):
            for i,acc in enumerate((self.model.val_true / self.model.val_total).cpu().numpy()):
                logs[f'acc/acc_{i}'] = acc
            for i,pacc in enumerate((self.model.pval_true / self.model.pval_total).cpu().numpy()):
                logs[f'acc/pacc_{i}'] = pacc
        
        self.log_dict(logs, sync_dist=True)
        return val_loss
    
    def test_step(self, val_batch, batch_idx):
        test_logits, test_loss = self.model.forward(*val_batch)

        self.log("test_loss", test_loss, sync_dist=True)
        return test_loss

# %% ../nbs/B2. Training (Lightning).ipynb 4
from fastcore.script import anno_parser
import shlex

# watch out: we can only pass Python values as keyword arguments (not positional)
# everything else has to be a string
def parse_and_call(name, fun, args, kwargs={}, log_to_wandb=True):
    p = anno_parser(fun)
    args = p.parse_args(args).__dict__
    args.pop('xtra'); args.pop('pdb')
    if log_to_wandb and type(wandb_logger.experiment.config) == wandb.sdk.wandb_config.Config:
        wandb_logger.experiment.config[name] = {k:v for k,v in args.items()}
    args.update({k:v for k, v in kwargs.items()})
    return fun(**args)

# %% ../nbs/B2. Training (Lightning).ipynb 7
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--task', type=str, help='Task to train')
parser.add_argument('--seed', type=int, default=0, help='Global training seed')
parser.add_argument('--batch-size', type=int, default=16, help='total batch size for all GPUs')
parser.add_argument('--workers', type=int, default=8, help='max dataloader workers (per RANK in DDP mode)')
parser.add_argument('--input-dir', type=str, default='', help='input data path') # fixed in the model for now
parser.add_argument("--checkpoint-dir", type=str, default="./checkpoints/", help="directory to save the checkpoints")
parser.add_argument('--epochs', type=int, default=10, help='total training epochs')
parser.add_argument('--validations_per_epoch', type=int, default=10, help='how many times to run validation during an epoch')
parser.add_argument('--weight-decay', type=float, default=1e-2, help='optimizer weight decay')
parser.add_argument('--lr0', type=float, default=1e-4, help='optimizer initial learning rate')
parser.add_argument('--clip-gradient-norm', type=float, default=None, help='enable gradient norm clipping')
parser.add_argument('--accumulate-grad-batches', type=int, default=1, help='perform the optimizer step only after going through several batches of samples')
parser.add_argument('--warmup-steps', type=int, default=10000, help='total number steps during which the learning rate rises (defaults to 10k updates)')
parser.add_argument('--tunables', type=str, default="", help='tunable hyperparameters')

args = parser.parse_args().__dict__

task_args: list = shlex.split(args.pop("task"))
task_name, task_args = task_args[0], task_args[1:]
input_args: list = shlex.split(args.pop("input_dir"))
checkpoint_dir: str = args.pop("checkpoint_dir")
num_workers: int = args.pop("workers")
batch_size: int = args.pop("batch_size")
epochs: int = args.pop("epochs")
validations_per_epoch: int = args.pop("validations_per_epoch")
tunables_args: list = shlex.split(args.pop("tunables"))

hyp_params = {}
hyp_params['warmup_steps'] = args['warmup_steps']
hyp_params['weight_decay'] = args['weight_decay']
hyp_params['clip_gradient_norm'] = args['clip_gradient_norm']
hyp_params['accumulate_grad_batches'] = args['accumulate_grad_batches']
hyp_params['lr0'] = args['lr0']
hyp_params['epochs'] = epochs

# %% ../nbs/B2. Training (Lightning).ipynb 8
from lightning.pytorch.loggers import WandbLogger
from lightning.pytorch.callbacks import LearningRateMonitor
import importlib

torch.set_float32_matmul_precision('medium')

wandb_logger = WandbLogger(project=f"SpearTTS-{task_name}")

ckpt_callback = pl.callbacks.ModelCheckpoint(
     dirpath=f'{task_name}-{epochs}e',
     filename=task_name+"-{epoch}-{step}-{val_loss:.2f}",
     monitor="val_loss",
     save_top_k=4,
     every_n_epochs=1,
 )

lr_monitor_callback = LearningRateMonitor(logging_interval='step')

from torch.utils.data import DataLoader

task = importlib.import_module("spear_tts_pytorch."+task_name)

train_ds, val_ds = parse_and_call('dataset', task.load_datasets, input_args)

tunables = None
if hasattr(task, "Tunables"):
    import dataclasses
    tunables = parse_and_call('tunables', task.Tunables, tunables_args, log_to_wandb=False)
    if type(wandb_logger.experiment.config) == wandb.sdk.wandb_config.Config:
        wandb_logger.experiment.config['tunables'] = dataclasses.asdict(tunables)

    print("Tunables:", tunables)
    for name in ["lr0", "clip_gradient_norm", "weight_decay", "warmup_steps"]:
        val = getattr(tunables, name, None)
        if val is not None: hyp_params[name] = val

val_loader = DataLoader(val_ds,
    batch_size=batch_size,
    num_workers=num_workers,
    drop_last=False,
    pin_memory=True)

train_loader = DataLoader(train_ds,
    batch_size=batch_size,
    num_workers=num_workers,
    drop_last=False,
    shuffle=True,
    pin_memory=True)

model_kwargs = dict(dataset=train_ds)
if tunables is not None: model_kwargs['tunables'] = tunables
model = parse_and_call('model', task.make_model, task_args, model_kwargs)

task = TrainingTask(model, model_hparams=hyp_params)

trainer = pl.Trainer(max_epochs=hyp_params['epochs'],
                  accelerator="gpu",
                  profiler="simple",
                  precision='16-mixed',
                  gradient_clip_val=hyp_params['clip_gradient_norm'],
                  accumulate_grad_batches=hyp_params['accumulate_grad_batches'],
                  val_check_interval=1/validations_per_epoch,
                  enable_checkpointing=True,
                  logger=wandb_logger,
                  callbacks=[ckpt_callback, lr_monitor_callback])

if type(wandb_logger.experiment.config) == wandb.sdk.wandb_config.Config:
    wandb_logger.experiment.config.update(hyp_params)

trainer.fit(model=task, train_dataloaders=train_loader, val_dataloaders=val_loader)
