from typing import Union

from nnunetv2.training.loss.deep_supervision import DeepSupervisionWrapper
from nnunetv2.training.loss.dice import DC_and_BCE_loss, DC_and_CE_loss
from nnunetv2.training.nnUNetTrainer.nnUNetTrainer import nnUNetTrainer
import numpy as np


class nnUNetTrainer_switchToDiceep800(nnUNetTrainer):
    def __init__(self, plans: dict, configuration: str, fold: int, dataset_json: dict, unpack_dataset: bool = True,
                 device: str = 'cuda'):
        super().__init__(plans, configuration, fold, dataset_json, unpack_dataset, device)
        self.switch_epoch = 800

    def build_loss_no_ce(self):
        if self.label_manager.has_regions:
            loss = DC_and_BCE_loss({},
                                   {'batch_dice': self.plans['configurations'][self.configuration]['batch_dice'],
                                    'do_bg': True, 'smooth': 1e-5},
                                   use_ignore_label=self.label_manager.ignore_label is not None, weight_ce=0)
        else:
            loss = DC_and_CE_loss({'batch_dice': self.plans['configurations'][self.configuration]['batch_dice'],
                                   'smooth': 1e-5, 'do_bg': False}, {}, weight_ce=0, weight_dice=1,
                                  ignore_label=self.label_manager.ignore_label)

        deep_supervision_scales = self._get_deep_supervision_scales()

        # we give each output a weight which decreases exponentially (division by 2) as the resolution decreases
        # this gives higher resolution outputs more weight in the loss
        weights = np.array([1 / (2 ** i) for i in range(len(deep_supervision_scales))])

        # we don't use the lowest 2 outputs. Normalize weights so that they sum to 1
        weights = weights / weights.sum()
        # now wrap the loss
        loss = DeepSupervisionWrapper(loss, weights)
        return loss

    def on_epoch_end(self):
        super().on_epoch_end()
        if self.current_epoch == self.switch_epoch:
            self.loss = self.build_loss_no_ce()
            self.print_to_log_file(f'Switched to Dice Loss only! {self.loss}, {self.loss.loss.weight_ce}')

    def load_checkpoint(self, filename_or_checkpoint: Union[dict, str]) -> None:
        super().load_checkpoint(filename_or_checkpoint)
        if self.current_epoch >= self.switch_epoch:
            self.loss = self.build_loss_no_ce()


class nnUNetTrainer_switchToDiceep100(nnUNetTrainer_switchToDiceep800):
    def __init__(self, plans: dict, configuration: str, fold: int, dataset_json: dict, unpack_dataset: bool = True,
                 device: str = 'cuda:0'):
        super().__init__(plans, configuration, fold, dataset_json, unpack_dataset, device)
        self.switch_epoch = 100


class nnUNetTrainer_switchToDiceep100noSmooth(nnUNetTrainer_switchToDiceep100):
    def build_loss_no_ce(self):
        if self.label_manager.has_regions:
            loss = DC_and_BCE_loss({},
                                   {'batch_dice': self.plans['configurations'][self.configuration]['batch_dice'],
                                    'do_bg': True, 'smooth': 0},
                                   use_ignore_label=self.label_manager.ignore_label is not None, weight_ce=0)
        else:
            loss = DC_and_CE_loss({'batch_dice': self.plans['configurations'][self.configuration]['batch_dice'],
                                   'smooth': 0, 'do_bg': False}, {}, weight_ce=0, weight_dice=1,
                                  ignore_label=self.label_manager.ignore_label)

        deep_supervision_scales = self._get_deep_supervision_scales()

        # we give each output a weight which decreases exponentially (division by 2) as the resolution decreases
        # this gives higher resolution outputs more weight in the loss
        weights = np.array([1 / (2 ** i) for i in range(len(deep_supervision_scales))])

        # we don't use the lowest 2 outputs. Normalize weights so that they sum to 1
        weights = weights / weights.sum()
        # now wrap the loss
        loss = DeepSupervisionWrapper(loss, weights)
        return loss