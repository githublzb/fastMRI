from collections import defaultdict

import numpy as np
import pytorch_lightning as pl
import torch
import torchvision
from torch.utils.data import DistributedSampler, DataLoader

from common import evaluate
from common.utils import save_reconstructions
from data.mri_data import SliceData


class MRIModel(pl.LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.hparams = hparams

    def _create_data_loader(self, data_transform, data_partition, sample_rate=None):
        sample_rate = sample_rate or self.hparams.sample_rate
        dataset = SliceData(
            root=self.hparams.data_path / f'{self.hparams.challenge}_{data_partition}',
            transform=data_transform,
            sample_rate=sample_rate,
            challenge=self.hparams.challenge
        )
        sampler = DistributedSampler(dataset)
        return DataLoader(
            dataset=dataset,
            batch_size=self.hparams.batch_size,
            num_workers=8,
            pin_memory=True,
            sampler=sampler,
        )

    def train_data_transform(self):
        raise NotImplementedError

    @pl.data_loader
    def train_dataloader(self):
        return self._create_data_loader(self.train_data_transform(), data_partition='train')

    def val_data_transform(self):
        raise NotImplementedError

    @pl.data_loader
    def val_dataloader(self):
        return self._create_data_loader(self.val_data_transform(), data_partition='val')

    def test_data_transform(self):
        raise NotImplementedError

    @pl.data_loader
    def test_dataloader(self):
        return self._create_data_loader(self.test_data_transform(), data_partition='test', sample_rate=1.)

    def _evaluate(self, val_logs):
        losses = []
        outputs = defaultdict(list)
        targets = defaultdict(list)
        for log in val_logs:
            losses.append(log['val_loss'].cpu().numpy())
            for i, (fname, slice) in enumerate(zip(log['fname'], log['slice'])):
                outputs[fname].append((slice, log['output'][i]))
                targets[fname].append((slice, log['target'][i]))
        metrics = dict(val_loss=losses, nmse=[], ssim=[], psnr=[])
        for fname in outputs:
            output = np.stack([out for _, out in sorted(outputs[fname])])
            target = np.stack([tgt for _, tgt in sorted(targets[fname])])
            metrics['nmse'].append(evaluate.nmse(target, output))
            metrics['ssim'].append(evaluate.ssim(target, output))
            metrics['psnr'].append(evaluate.psnr(target, output))
        metrics = {metric: np.mean(values) for metric, values in metrics.items()}
        print(metrics, '\n')
        return dict(log=metrics, **metrics)

    def _visualize(self, val_logs):
        def _normalize(image):
            image = image[np.newaxis]
            image -= image.min()
            return image / image.max()

        def save_image(image, tag):
            grid = torchvision.utils.make_grid(torch.Tensor(image), nrow=4, pad_value=1)
            self.logger.experiment.add_image(tag, grid)

        num_logs = len(val_logs)
        step = (num_logs + 15) // 16
        outputs, targets = [], []
        for i in range(0, num_logs, step):
            outputs.append(_normalize(val_logs[i]['output'][0]))
            targets.append(_normalize(val_logs[i]['target'][0]))
        outputs = np.stack(outputs)
        targets = np.stack(targets)
        save_image(targets, 'Target')
        save_image(outputs, 'Reconstruction')
        save_image(np.abs(targets - outputs), 'Error')

    def validation_end(self, val_logs):
        self._visualize(val_logs)
        return self._evaluate(val_logs)

    def test_end(self, test_logs):
        outputs = defaultdict(list)
        for log in test_logs:
            for i, (fname, slice) in enumerate(zip(log['fname'], log['slice'])):
                outputs[fname].append((slice, log['output'][i]))
        for fname in outputs:
            outputs[fname] = np.stack([out for _, out in sorted(outputs[fname])])
        save_reconstructions(outputs, self.hparams.exp_dir / self.hparams.exp / 'reconstructions')
        return dict()
