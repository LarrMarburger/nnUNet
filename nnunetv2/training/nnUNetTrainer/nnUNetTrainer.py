import inspect
import os
import shutil
import sys
from copy import deepcopy
from datetime import datetime
from time import time, sleep
from typing import Union, Tuple, List

import matplotlib
import numpy as np
import torch
from batchgenerators.transforms.abstract_transforms import AbstractTransform, Compose
from batchgenerators.transforms.color_transforms import BrightnessMultiplicativeTransform, \
    ContrastAugmentationTransform, GammaTransform
from batchgenerators.transforms.noise_transforms import GaussianNoiseTransform, GaussianBlurTransform
from batchgenerators.transforms.resample_transforms import SimulateLowResolutionTransform
from batchgenerators.transforms.spatial_transforms import SpatialTransform, MirrorTransform
from batchgenerators.transforms.utility_transforms import RemoveLabelTransform, RenameTransform, NumpyToTensor
from batchgenerators.utilities.file_and_folder_operations import join, load_json, isfile, save_json, maybe_mkdir_p
from nnunetv2.configuration import ANISO_THRESHOLD, default_num_processes
from nnunetv2.paths import nnUNet_preprocessed, nnUNet_results
from nnunetv2.training.data_augmentation.compute_initial_patch_size import get_patch_size
from nnunetv2.training.data_augmentation.custom_transforms.cascade_transforms import MoveSegAsOneHotToData, \
    ApplyRandomBinaryOperatorTransform, RemoveRandomConnectedComponentFromOneHotEncodingTransform
from nnunetv2.training.data_augmentation.custom_transforms.deep_supervision_donwsampling import \
    DownsampleSegForDSTransform2
from nnunetv2.training.data_augmentation.custom_transforms.limited_length_multithreaded_augmenter import \
    LimitedLenWrapper
from nnunetv2.training.data_augmentation.custom_transforms.masking import MaskTransform
from nnunetv2.training.data_augmentation.custom_transforms.region_based_training import \
    ConvertSegmentationToRegionsTransform
from nnunetv2.training.data_augmentation.custom_transforms.transforms_for_dummy_2d import Convert2DTo3DTransform, \
    Convert3DTo2DTransform
from nnunetv2.training.dataloading.data_loader_2d import nnUNetDataLoader2D
from nnunetv2.training.dataloading.data_loader_3d import nnUNetDataLoader3D
from nnunetv2.training.dataloading.nnunet_dataset import nnUNetDataset
from nnunetv2.training.dataloading.utils import get_case_identifiers, unpack_dataset
from nnunetv2.training.loss.deep_supervision import DeepSupervisionWrapper
from nnunetv2.training.loss.dice import DC_and_CE_loss, DC_and_BCE_loss, get_tp_fp_fn_tn
from nnunetv2.training.lr_scheduler.polylr import PolyLRScheduler
from nnunetv2.utilities.collate_outputs import collate_outputs
from nnunetv2.utilities.default_n_proc_DA import get_allowed_n_proc_DA
from nnunetv2.utilities.get_network_from_plans import get_network_from_plans
from nnunetv2.utilities.helpers import softmax_helper_dim0
from nnunetv2.utilities.label_handling import handle_labels
from sklearn.model_selection import KFold
from torch import autocast
from torch.cuda.amp import GradScaler

matplotlib.use('agg')
import seaborn as sns
import matplotlib.pyplot as plt



class nnUNetTrainer(object):
    def __init__(self, plans: dict, configuration: str, fold: int, dataset_json: dict, unpack_dataset: bool = True,
                 device: str = 'cuda:0'):
        # From https://grugbrain.dev/. Worth a read ya filthy big brains ;-)

        # apex predator of grug is complexity
        # complexity bad
        # say again:
        # complexity very bad
        # you say now:
        # complexity very, very bad
        # given choice between complexity or one on one against t-rex, grug take t-rex: at least grug see t-rex
        # complexity is spirit demon that enter codebase through well-meaning but ultimately very clubbable non grug-brain developers and project managers who not fear complexity spirit demon or even know about sometime
        # one day code base understandable and grug can get work done, everything good!
        # next day impossible: complexity demon spirit has entered code and very dangerous situation!

        # loading and saving this class for continuing from checkpoint should not happen based on pickling. This
        # would also pickle the network etc. Bad, bad. Instead we just reinstantiate and then load the checkpoint we
        # need. So let's save the init args
        self.my_init_kwargs = {}
        for k in inspect.signature(self.__init__).parameters.keys():
            self.my_init_kwargs[k] = locals()[k]

        ###  Saving all the init args into class variables for later access
        self.plans = plans
        self.dataset_json = dataset_json
        self.configuration = configuration
        self.fold = fold
        self.unpack_dataset = unpack_dataset
        self.device = device
        # autograd needs this
        self.device_type = device[:-2] if device.startswith('cuda') else device

        ### Setting all the folder names. We need to make sure things don't crash in case we are just running
        # inference and some of the folders may not be defined!
        self.preprocessed_dataset_folder_base = join(nnUNet_preprocessed, plans['dataset_name']) \
            if nnUNet_preprocessed is not None else None
        self.output_folder_base = join(nnUNet_results, plans['dataset_name'],
                                       self.__class__.__name__ + '__' + plans['plans_name'] + "__" + configuration)\
            if nnUNet_results is not None else None
        self.output_folder = join(self.output_folder_base, f'fold_{fold}')

        self.preprocessed_dataset_folder = join(self.preprocessed_dataset_folder_base,
                                                self.plans['configurations'][self.configuration]["data_identifier"])
        # unlike the previous nnunet folder_with_segs_from_previous_stage is now part of the plans. For now it has to
        # be a different configuration in the same plans
        # IMPORTANT! the mapping must be bijective, so lowres must point to fullres and vice versa (using
        # "previous_stage" and "next_stage"). Otherwise it won't work!
        # TODO: allow multiple next stages for one configuration
        self.folder_with_segs_from_previous_stage = \
            join(nnUNet_results, plans['dataset_name'],
                 self.__class__.__name__ + '__' + plans['plans_name'] + "__" +
                 plans['configurations'][configuration]['previous_stage'], 'prediction_next_stage') \
            if 'previous_stage' in plans['configurations'][configuration].keys() else None

        ### Some hyperparameters for you to fiddle with
        self.initial_lr = 1e-2
        self.weight_decay = 3e-5
        self.oversample_foreground_percent = 0.33
        self.num_iterations_per_epoch = 250
        self.num_val_iterations_per_epoch = 50
        self.num_epochs = 1000
        self.current_epoch = 0

        ### Dealing with labels/regions
        # labels can either be a list of int (regular training) or a list of tuples of int (region-based training)
        self.labels, self.regions, self.ignore_label = handle_labels(self.dataset_json)
        # needed for predictions. We do sigmoid in case of (overlapping) regions
        self.inference_nonlinearity = torch.sigmoid if self.regions is not None else softmax_helper_dim0

        # if you want to swap out the network architecture you need to change that here. You do not need to use the
        # plans.json file at all if you don't want to, just make sure your architecture is compatible with the patch
        # size dictated by the plans!
        self.network = get_network_from_plans(self.plans, self.dataset_json, self.configuration).to(self.device)
        self.optimizer, self.lr_scheduler = self.configure_optimizers()
        self.grad_scaler = GradScaler() if self.device_type == 'cuda' else None

        self.loss = self._build_loss()

        ### Simple logging. Don't take that away from me!
        # initialize log file. This is just our log for the print statements etc. Not to be confused with lightning
        # logging
        timestamp = datetime.now()
        self.log_file = join(self.output_folder, "training_log_%d_%d_%d_%02.0d_%02.0d_%02.0d.txt" %
                             (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute,
                              timestamp.second))
        self.my_fantastic_logging = {
            'mean_fg_dice': list(),
            'dice_per_class_or_region': list(),
            'train_losses': list(),
            'val_losses': list(),
            'lrs': list(),
            'epoch_start_timestamps': list(),
            'epoch_end_timestamps': list()
        }
        # shut up, this logging is great

        ### placeholders
        self.dataloader_train = self.dataloader_val = None  # see on_train_start

        ### initializing stuff for remembering things and such
        self._ema_pseudo_dice = None
        self._best_ema = None

        ### inference things
        self.inference_allowed_mirroring_axes = None  # this variable is set in
        # self.configure_rotation_dummyDA_mirroring_and_inital_patch_size and will be saved in checkpoints
        self.inference_gaussian = None  # will be set in self.on_predict_start to speed up inference. After
        # prediction it is set back to None in self.on_predict_end
        self.inference_segmentation_export_pool = None
        self.inference_parameters = {
            'tile_step_size': 0.5,
            'use_gaussian': True,
            'use_mirroring': True,
            'perform_everything_on_gpu': False,
            'verbose': True,
            'save_probabilities': False,
            'n_processes_segmentation_export': default_num_processes,
        }

        ### checkpoint saving stuff
        self.save_every = 50
        self.disable_checkpointing = False

    def _get_deep_supervision_scales(self):
        deep_supervision_scales = list(list(i) for i in 1 / np.cumprod(np.vstack(
            self.plans['configurations'][self.configuration]['pool_op_kernel_sizes']), axis=0))[:-1]
        return deep_supervision_scales

    def _build_loss(self):
        if self.regions is None:
            loss = DC_and_CE_loss({'batch_dice': self.plans['configurations'][self.configuration]['batch_dice'],
                                        'smooth': 1e-5, 'do_bg': False}, {}, weight_ce=1, weight_dice=1,
                                       ignore_label=self.ignore_label)
        else:
            loss = DC_and_BCE_loss({},
                                        {'batch_dice': self.plans['configurations'][self.configuration]['batch_dice'],
                                         'do_bg': True, 'smooth': 1e-5}, ignore_label=self.ignore_label)
        deep_supervision_scales = self._get_deep_supervision_scales()

        # we give each output a weight which decreases exponentially (division by 2) as the resolution decreases
        # this gives higher resolution outputs more weight in the loss
        weights = np.array([1 / (2 ** i) for i in range(len(deep_supervision_scales))])

        # we don't use the lowest 2 outputs. Normalize weights so that they sum to 1
        weights = weights / weights.sum()
        # now wrap the loss
        loss = DeepSupervisionWrapper(loss, weights)
        return loss

    def configure_rotation_dummyDA_mirroring_and_inital_patch_size(self):
        """
        This function is stupid and certainly one of the weakest spots of this implementation. Not entirely sure how we can fix it.
        """
        patch_size = self.plans['configurations'][self.configuration]["patch_size"]
        dim = len(patch_size)
        # todo rotation should be defined dynamically based on patch size (more isotropic patch sizes = more rotation)
        if dim == 2:
            do_dummy_2d_data_aug = False
            # todo revisit this parametrization
            if max(self.plans['configurations'][self.configuration]['patch_size']) / \
                    min(self.plans['configurations'][self.configuration]['patch_size']) > 1.5:
                rotation_for_DA = {
                    'x': (-15. / 360 * 2. * np.pi, 15. / 360 * 2. * np.pi),
                    'y': (0, 0),
                    'z': (0, 0)
                }
            else:
                rotation_for_DA = {
                    'x': (-180. / 360 * 2. * np.pi, 180. / 360 * 2. * np.pi),
                    'y': (0, 0),
                    'z': (0, 0)
                }
            mirror_axes = (0, 1)
        elif dim == 3:
            # todo this is not ideal. We could also have patch_size (64, 16, 128) in which case a full 180deg 2d rot would be bad
            # order of the axes is determined by spacing, not image size
            do_dummy_2d_data_aug = (max(patch_size) / patch_size[0]) > ANISO_THRESHOLD
            if do_dummy_2d_data_aug:
                # why do we rotate 180 deg here all the time? We should also restrict it
                rotation_for_DA = {
                    'x': (-180. / 360 * 2. * np.pi, 180. / 360 * 2. * np.pi),
                    'y': (0, 0),
                    'z': (0, 0)
                }
            else:
                rotation_for_DA = {
                    'x': (-30. / 360 * 2. * np.pi, 30. / 360 * 2. * np.pi),
                    'y': (-30. / 360 * 2. * np.pi, 30. / 360 * 2. * np.pi),
                    'z': (-30. / 360 * 2. * np.pi, 30. / 360 * 2. * np.pi),
                }
            mirror_axes = (0, 1, 2)
        else:
            raise RuntimeError()

        # todo this function is stupid. It doesn't even use the correct scale range (we keep things as they were in the
        #  old nnunet for now)
        initial_patch_size = get_patch_size(patch_size[-dim:],
                                            *rotation_for_DA.values(),
                                            (0.85, 1.25))
        if do_dummy_2d_data_aug:
            initial_patch_size[0] = patch_size[0]

        self.print_to_log_file(f'do_dummy_2d_data_aug: {do_dummy_2d_data_aug}')
        self.inference_allowed_mirroring_axes = mirror_axes

        return rotation_for_DA, do_dummy_2d_data_aug, initial_patch_size, mirror_axes

    def print_to_log_file(self, *args, also_print_to_console=True, add_timestamp=True):
        timestamp = time()
        dt_object = datetime.fromtimestamp(timestamp)

        if add_timestamp:
            args = ("%s:" % dt_object, *args)

        successful = False
        max_attempts = 5
        ctr = 0
        while not successful and ctr < max_attempts:
            try:
                with open(self.log_file, 'a+') as f:
                    for a in args:
                        f.write(str(a))
                        f.write(" ")
                    f.write("\n")
                successful = True
            except IOError:
                print("%s: failed to log: " % datetime.fromtimestamp(timestamp), sys.exc_info())
                sleep(0.5)
                ctr += 1
        if also_print_to_console:
            print(*args)

    def print_plans(self):
        dct = deepcopy(self.plans)
        config = dct['configurations'][self.configuration]
        del dct['configurations']
        self.print_to_log_file('\n##################\nThis is the configuration used by this training:\n', config, '\n')
        self.print_to_log_file('These are the global plan.json settings:\n', dct, '\n', add_timestamp=False)

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.network.parameters(), self.initial_lr, weight_decay=self.weight_decay,
                                    momentum=0.99, nesterov=True)
        lr_scheduler = PolyLRScheduler(optimizer, self.initial_lr, self.num_epochs)
        return optimizer, lr_scheduler

    def plot_network_architecture(self):
        try:
            from batchgenerators.utilities.file_and_folder_operations import join
            import hiddenlayer as hl
            g = hl.build_graph(self.network,
                               torch.rand((1, len(self.dataset_json["modality"]),
                                           *self.plans['configurations'][self.configuration]['patch_size']),
                                          device=self.device),
                               transforms=None)
            g.save(join(self.output_folder, "network_architecture.pdf"))
            del g
        except Exception as e:
            self.print_to_log_file("Unable to plot network architecture:")
            self.print_to_log_file(e)

            self.print_to_log_file("\nprinting the network instead:\n")
            self.print_to_log_file(self.network)
            self.print_to_log_file("\n")
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def do_split(self):
        """
        The default split is a 5 fold CV on all available training cases. nnU-Net will create a split (it is seeded,
        so always the same) and save it as splits_final.pkl file in the preprocessed data directory.
        Sometimes you may want to create your own split for various reasons. For this you will need to create your own
        splits_final.pkl file. If this file is present, nnU-Net is going to use it and whatever splits are defined in
        it. You can create as many splits in this file as you want. Note that if you define only 4 splits (fold 0-3)
        and then set fold=4 when training (that would be the fifth split), nnU-Net will print a warning and proceed to
        use a random 80:20 data split.
        :return:
        """

        if self.fold == "all":
            # if fold==all then we use all images for training and validation
            case_identifiers = get_case_identifiers(self.preprocessed_dataset_folder)
            tr_keys = case_identifiers
            val_keys = []
        else:
            splits_file = join(self.preprocessed_dataset_folder_base, "splits_final.json")
            dataset = nnUNetDataset(self.preprocessed_dataset_folder, case_identifiers=None,
                                    num_cases_properties_loading_threshold=0,
                                    folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage)
            # if the split file does not exist we need to create it
            if not isfile(splits_file):
                self.print_to_log_file("Creating new 5-fold cross-validation split...")
                splits = []
                all_keys_sorted = np.sort(list(dataset.keys()))
                kfold = KFold(n_splits=5, shuffle=True, random_state=12345)
                for i, (train_idx, test_idx) in enumerate(kfold.split(all_keys_sorted)):
                    train_keys = np.array(all_keys_sorted)[train_idx]
                    test_keys = np.array(all_keys_sorted)[test_idx]
                    splits.append({})
                    splits[-1]['train'] = list(train_keys)
                    splits[-1]['val'] = list(test_keys)
                save_json(splits, splits_file)

            else:
                self.print_to_log_file("Using splits from existing split file:", splits_file)
                splits = load_json(splits_file)
                self.print_to_log_file("The split file contains %d splits." % len(splits))

            self.print_to_log_file("Desired fold for training: %d" % self.fold)
            if self.fold < len(splits):
                tr_keys = splits[self.fold]['train']
                val_keys = splits[self.fold]['val']
                self.print_to_log_file("This split has %d training and %d validation cases."
                                       % (len(tr_keys), len(val_keys)))
            else:
                self.print_to_log_file("INFO: You requested fold %d for training but splits "
                                       "contain only %d folds. I am now creating a "
                                       "random (but seeded) 80:20 split!" % (self.fold, len(splits)))
                # if we request a fold that is not in the split file, create a random 80:20 split
                rnd = np.random.RandomState(seed=12345 + self.fold)
                keys = np.sort(list(dataset.keys()))
                idx_tr = rnd.choice(len(keys), int(len(keys) * 0.8), replace=False)
                idx_val = [i for i in range(len(keys)) if i not in idx_tr]
                tr_keys = [keys[i] for i in idx_tr]
                val_keys = [keys[i] for i in idx_val]
                self.print_to_log_file("This random 80:20 split has %d training and %d validation cases."
                                       % (len(tr_keys), len(val_keys)))
        if any([i in val_keys for i in tr_keys]):
            self.print_to_log_file('WARNING: Some validation cases are also in the training set. Please check the '
                                   'splits.json or ignore if this is intentional.')
        return tr_keys, val_keys

    def get_tr_and_val_datasets(self):
        # create dataset split
        tr_keys, val_keys = self.do_split()

        # load the datasets for training and validation. Note that we always draw random samples so we really don't
        # care about distributing training cases across GPUs.
        dataset_tr = nnUNetDataset(self.preprocessed_dataset_folder, tr_keys,
                                   folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage)
        dataset_val = nnUNetDataset(self.preprocessed_dataset_folder, val_keys,
                                    folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage)
        return dataset_tr, dataset_val

    def get_dataloaders(self):
        # we use the patch size to determine whether we need 2D or 3D dataloaders. We also use it to determine whether
        # we need to use dummy 2D augmentation (in case of 3D training) and what our initial patch size should be
        patch_size = self.plans['configurations'][self.configuration]["patch_size"]
        dim = len(patch_size)

        # needed for deep supervision: how much do we need to downscale the segmentation targets for the different
        # outputs?
        deep_supervision_scales = self._get_deep_supervision_scales()

        rotation_for_DA, do_dummy_2d_data_aug, initial_patch_size, mirror_axes = \
            self.configure_rotation_dummyDA_mirroring_and_inital_patch_size()

        # training pipeline
        tr_transforms = self.get_training_transforms(
            patch_size, rotation_for_DA, deep_supervision_scales, mirror_axes, do_dummy_2d_data_aug,
            order_resampling_data=3, order_resampling_seg=1,
            use_mask_for_norm=self.plans['configurations'][self.configuration]['use_mask_for_norm'],
            is_cascaded=False, all_labels=self.labels, regions=self.regions)

        # validation pipeline
        val_transforms = self.get_validation_transforms(deep_supervision_scales,
                                                        is_cascaded=False,
                                                        all_labels=self.labels,
                                                        regions=self.regions)

        dl_tr, dl_val = self.get_plain_dataloaders(initial_patch_size, dim)

        allowed_num_processes = get_allowed_n_proc_DA()
        mt_gen_train = LimitedLenWrapper(self.num_iterations_per_epoch, dl_tr, tr_transforms,
                                         allowed_num_processes, 1, None, True, 0.02)
        mt_gen_val = LimitedLenWrapper(self.num_val_iterations_per_epoch, dl_val, val_transforms,
                                       max(1, allowed_num_processes // 2), 1, None, True, 0.02)

        return mt_gen_train, mt_gen_val

    def get_plain_dataloaders(self, initial_patch_size: Tuple[int, ...], dim: int):
        dataset_tr, dataset_val = self.get_tr_and_val_datasets()

        if dim == 2:
            dl_tr = nnUNetDataLoader2D(dataset_tr, self.plans['configurations'][self.configuration]['batch_size'],
                                       initial_patch_size,
                                       self.plans['configurations'][self.configuration]['patch_size'],
                                       oversample_foreground_percent=self.oversample_foreground_percent,
                                       sampling_probabilities=None, pad_sides=None)
            dl_val = nnUNetDataLoader2D(dataset_val, self.plans['configurations'][self.configuration]['batch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        oversample_foreground_percent=self.oversample_foreground_percent,
                                        sampling_probabilities=None, pad_sides=None)
        else:
            dl_tr = nnUNetDataLoader3D(dataset_tr, self.plans['configurations'][self.configuration]['batch_size'],
                                       initial_patch_size,
                                       self.plans['configurations'][self.configuration]['patch_size'],
                                       oversample_foreground_percent=self.oversample_foreground_percent,
                                       sampling_probabilities=None, pad_sides=None)
            dl_val = nnUNetDataLoader3D(dataset_val, self.plans['configurations'][self.configuration]['batch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        oversample_foreground_percent=self.oversample_foreground_percent,
                                        sampling_probabilities=None, pad_sides=None)
        return dl_tr, dl_val

    @staticmethod
    def get_training_transforms(patch_size: Union[np.ndarray, Tuple[int]],
                                rotation_for_DA: dict,
                                deep_supervision_scales: Union[List, Tuple],
                                mirror_axes: Tuple[int, ...],
                                do_dummy_2d_data_aug: bool,
                                order_resampling_data: int = 1,
                                order_resampling_seg: int = 0,
                                border_val_seg: int = -1,
                                use_mask_for_norm: List[bool] = None,
                                is_cascaded: bool = False,
                                all_labels: Union[Tuple[int, ...], List[int]] = None,
                                regions: List[Union[List[int], Tuple[int, ...]]] = None) -> AbstractTransform:
        if is_cascaded and regions is not None:
            raise NotImplementedError('Region based training is not yet implemented for the cascade!')

        tr_transforms = []
        if do_dummy_2d_data_aug:
            ignore_axes = (0,)
            tr_transforms.append(Convert3DTo2DTransform())
            patch_size_spatial = patch_size[1:]
        else:
            patch_size_spatial = patch_size
            ignore_axes = None

        tr_transforms.append(SpatialTransform(
            patch_size_spatial, patch_center_dist_from_border=None,
            do_elastic_deform=False, alpha=(0, 0), sigma=(0, 0),
            do_rotation=True, angle_x=rotation_for_DA['x'], angle_y=rotation_for_DA['y'], angle_z=rotation_for_DA['z'],
            p_rot_per_axis=1,  # todo experiment with this
            do_scale=True, scale=(0.7, 1.4),
            border_mode_data="constant", border_cval_data=0, order_data=order_resampling_data,
            border_mode_seg="constant", border_cval_seg=border_val_seg, order_seg=order_resampling_seg,
            random_crop=False,  # random cropping is part of our dataloaders
            p_el_per_sample=0, p_scale_per_sample=0.2, p_rot_per_sample=0.2,
            independent_scale_for_each_axis=False  # todo experiment with this
        ))

        if do_dummy_2d_data_aug:
            tr_transforms.append(Convert2DTo3DTransform())

        tr_transforms.append(GaussianNoiseTransform(p_per_sample=0.1))
        tr_transforms.append(GaussianBlurTransform((0.5, 1.), different_sigma_per_channel=True, p_per_sample=0.2,
                                                   p_per_channel=0.5))
        tr_transforms.append(BrightnessMultiplicativeTransform(multiplier_range=(0.75, 1.25), p_per_sample=0.15))
        tr_transforms.append(ContrastAugmentationTransform(p_per_sample=0.15))
        tr_transforms.append(SimulateLowResolutionTransform(zoom_range=(0.5, 1), per_channel=True,
                                                            p_per_channel=0.5,
                                                            order_downsample=0, order_upsample=3, p_per_sample=0.25,
                                                            ignore_axes=ignore_axes))
        tr_transforms.append(GammaTransform((0.7, 1.5), True, True, retain_stats=True, p_per_sample=0.1))
        tr_transforms.append(GammaTransform((0.7, 1.5), False, True, retain_stats=True, p_per_sample=0.3))

        if len(mirror_axes) > 0:
            tr_transforms.append(MirrorTransform(mirror_axes))

        if use_mask_for_norm is not None and any(use_mask_for_norm):
            tr_transforms.append(MaskTransform([i for i in range(len(use_mask_for_norm)) if use_mask_for_norm[i]],
                                               mask_idx_in_seg=0, set_outside_to=0))

        tr_transforms.append(RemoveLabelTransform(-1, 0))

        if is_cascaded:
            assert all_labels is not None, 'We need all_labels for cascade augmentations'
            tr_transforms.append(MoveSegAsOneHotToData(1, all_labels, 'seg', 'data'))
            tr_transforms.append(ApplyRandomBinaryOperatorTransform(
                channel_idx=list(range(-len(all_labels), 0)),
                p_per_sample=0.4,
                key="data",
                strel_size=(1, 8),
                p_per_label=1))
            tr_transforms.append(
                RemoveRandomConnectedComponentFromOneHotEncodingTransform(
                    channel_idx=list(range(-len(all_labels), 0)),
                    key="data",
                    p_per_sample=0.2,
                    fill_with_other_class_p=0,
                    dont_do_if_covers_more_than_x_percent=0.15))

        tr_transforms.append(RenameTransform('seg', 'target', True))

        if regions is not None:
            tr_transforms.append(ConvertSegmentationToRegionsTransform(regions, 'target', 'target'))

        if deep_supervision_scales is not None:
            tr_transforms.append(DownsampleSegForDSTransform2(deep_supervision_scales, 0, input_key='target',
                                                              output_key='target'))
        tr_transforms.append(NumpyToTensor(['data', 'target'], 'float'))
        tr_transforms = Compose(tr_transforms)
        return tr_transforms

    @staticmethod
    def get_validation_transforms(deep_supervision_scales: Union[List, Tuple],
                                  is_cascaded: bool = False,
                                  all_labels: Union[Tuple[int, ...], List[int]] = None,
                                  regions: List[Union[List[int], Tuple[int, ...]]] = None) -> AbstractTransform:
        if is_cascaded and regions is not None:
            raise NotImplementedError('Region based training is not yet implemented for the cascade!')

        val_transforms = []
        val_transforms.append(RemoveLabelTransform(-1, 0))

        if is_cascaded:
            val_transforms.append(MoveSegAsOneHotToData(1, all_labels, 'seg', 'data'))

        val_transforms.append(RenameTransform('seg', 'target', True))

        if regions is not None:
            val_transforms.append(ConvertSegmentationToRegionsTransform(regions, 'target', 'target'))

        if deep_supervision_scales is not None:
            val_transforms.append(DownsampleSegForDSTransform2(deep_supervision_scales, 0, input_key='target',
                                                               output_key='target'))

        val_transforms.append(NumpyToTensor(['data', 'target'], 'float'))
        val_transforms = Compose(val_transforms)
        return val_transforms

    def on_train_start(self):
        self.print_plans()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        maybe_mkdir_p(self.output_folder)

        # maybe unpack
        if self.unpack_dataset:
            self.print_to_log_file('unpacking dataset...')
            unpack_dataset(self.preprocessed_dataset_folder, unpack_segmentation=True, overwrite_existing=False,
                           num_processes=max(1, get_allowed_n_proc_DA() // 2))
            self.print_to_log_file('unpacking done...')

        # dataloaders must be instantiated here because they need access to the training data which may not be present
        # when doing inference
        self.dataloader_train, self.dataloader_val = self.get_dataloaders()

        # copy plans and dataset.json so that they can be used for restoring everything we need for inference
        save_json(self.plans, join(self.output_folder_base, 'plans.json'))
        save_json(self.dataset_json, join(self.output_folder_base, 'dataset.json'))

        # we don't really need the fingerprint but its still handy to have it with the others
        shutil.copy(join(self.preprocessed_dataset_folder_base, 'dataset_fingerprint.json'),
                    join(self.output_folder_base, 'dataset_fingerprint.json'))

        # produces a pdf in output folder
        self.plot_network_architecture()

    def on_train_end(self):
        self.save_checkpoint(join(self.output_folder, "checkpoint_final.pth"))
        # now we can delete latest
        if isfile(join(self.output_folder, "checkpoint_latest.pth")):
            os.remove(join(self.output_folder, "checkpoint_latest.pth"))

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def on_train_epoch_start(self):
        self.lr_scheduler.step(self.current_epoch)
        self.print_to_log_file('')
        self.print_to_log_file(f'Epoch {self.current_epoch}')
        self.print_to_log_file(
            f"Current learning rate: {np.round(self.optimizer.param_groups[0]['lr'], decimals=5)}")
        self.my_fantastic_logging['lrs'].append(self.optimizer.param_groups[0]['lr'])

    def train_step(self, batch: dict) -> dict:
        data = batch['data']
        target = batch['target']

        data = data.to(self.device, non_blocking=True)
        if isinstance(target, list):
            target = [i.to(self.device, non_blocking=True) for i in target]
        else:
            target = target.to(self.device, non_blocking=True)

        self.optimizer.zero_grad()

        with autocast(self.device_type):
            output = self.network(data)
            del data
            l = self.loss(output, target)

        self.grad_scaler.scale(l).backward()
        self.grad_scaler.unscale_(self.optimizer)
        torch.nn.utils.clip_grad_norm_(self.network.parameters(), 12)
        self.grad_scaler.step(self.optimizer)
        self.grad_scaler.update()
        return {'loss': l.detach().cpu().numpy()}

    def on_train_epoch_end(self, train_outputs: List[dict]):
        outputs = collate_outputs(train_outputs)
        average_loss = np.mean(outputs['loss'])
        self.my_fantastic_logging['train_losses'].append(average_loss)

    def on_validation_epoch_start(self):
        pass

    def validation_step(self, batch: dict) -> dict:
        data = batch['data']
        target = batch['target']

        data = data.to(self.device, non_blocking=True)
        if isinstance(target, list):
            target = [i.to(self.device, non_blocking=True) for i in target]
        else:
            target = target.to(self.device, non_blocking=True)

        self.optimizer.zero_grad()

        with autocast(self.device_type):
            output = self.network(data)
            del data
            l = self.loss(output, target)

        # we only need the output with the highest output resolution
        output = output[0]
        target = target[0]

        # the following is needed for online evaluation. Fake dice (green line)
        axes = [0] + list(range(2, len(output.shape)))

        if self.regions is None:
            # no need for softmax
            output_seg = output.argmax(1)[:, None]
            predicted_segmentation_onehot = torch.zeros(output.shape, device=output.device, dtype=torch.float32)
            predicted_segmentation_onehot.scatter_(1, output_seg, 1)
            del output_seg
        else:
            predicted_segmentation_onehot = (torch.sigmoid(output) > 0.5).long()

        tp, fp, fn, _ = get_tp_fp_fn_tn(predicted_segmentation_onehot, target, axes=axes)

        # [1:] in order to remove background
        tp_hard = tp.detach().cpu().numpy()[1:]
        fp_hard = fp.detach().cpu().numpy()[1:]
        fn_hard = fn.detach().cpu().numpy()[1:]
        return {'loss': l.detach().cpu().numpy(), 'tp_hard': tp_hard, 'fp_hard': fp_hard, 'fn_hard': fn_hard}

    def on_validation_epoch_end(self, val_outputs: List[dict]):
        outputs_collated = collate_outputs(val_outputs)
        tp = np.sum(outputs_collated['tp_hard'], 0)
        fp = np.sum(outputs_collated['fp_hard'], 0)
        fn = np.sum(outputs_collated['fn_hard'], 0)
        global_dc_per_class = [i for i in [2 * i / (2 * i + j + k) for i, j, k in
                                           zip(tp, fp, fn)]]
        mean_fg_dice = np.nanmean(global_dc_per_class)
        self.my_fantastic_logging['mean_fg_dice'].append(mean_fg_dice)
        self.my_fantastic_logging['dice_per_class_or_region'].append(global_dc_per_class)
        self.my_fantastic_logging['val_losses'].append(np.mean(outputs_collated['loss']))

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def on_epoch_start(self):
        self.my_fantastic_logging['epoch_start_timestamps'].append(time())

    def on_epoch_end(self):
        self.my_fantastic_logging['epoch_end_timestamps'].append(time())

        self.print_to_log_file('train_loss', np.round(self.my_fantastic_logging['train_losses'][-1], decimals=4))
        self.print_to_log_file('val_loss', np.round(self.my_fantastic_logging['val_losses'][-1], decimals=4))
        self.print_to_log_file('Pseudo dice', [np.round(i, decimals=4) for i in
                                               self.my_fantastic_logging['dice_per_class_or_region'][-1]])
        self.print_to_log_file(
            f"Epoch time: {np.round(self.my_fantastic_logging['epoch_end_timestamps'][-1] - self.my_fantastic_logging['epoch_start_timestamps'][-1], decimals=2)} s")

        self._plot_progress_png()

        # handling periodic checkpointing
        current_epoch = self.current_epoch
        if not self.disable_checkpointing and \
                (current_epoch + 1) % self.save_every == 0 and current_epoch != (self.num_epochs - 1):
            self.save_checkpoint(join(self.output_folder, 'checkpoint_latest.pth'))
        elif not self.disable_checkpointing and \
                current_epoch == self.num_epochs - 1:
            self.save_checkpoint(join(self.output_folder, 'checkpoint_final.pth'),)
            # delete latest checkpoint
            if isfile(join(self.output_folder, 'checkpoint_latest.pth')):
                os.remove(join(self.output_folder, 'checkpoint_latest.pth'))

        # handle 'best' checkpointing
        # exponential moving average of pseudo dice to determine 'best' model (use with caution)
        self._ema_pseudo_dice = self._ema_pseudo_dice * 0.9 + 0.1 * self.my_fantastic_logging['mean_fg_dice'][-1] \
            if self._ema_pseudo_dice is not None else self.my_fantastic_logging['mean_fg_dice'][-1]

        if self._best_ema is None or self._ema_pseudo_dice > self._best_ema:
            self._best_ema = self._ema_pseudo_dice
            self.print_to_log_file(f"Yayy! New best EMA pseudo Dice: {np.round(self._best_ema, decimals=4)}")
            self.save_checkpoint(join(self.output_folder, 'checkpoint_best.pth'))

        self.current_epoch += 1

    def save_checkpoint(self, filename: str) -> None:
        checkpoint = {
            'network_weights': self.network.state_dict(),
            'optimizer_state': self.optimizer.state_dict(),
            'grad_scaler_state': self.grad_scaler.state_dict(),
            'logging': self.my_fantastic_logging,
            '_best_ema': self._best_ema,
            '_current_ema': self._ema_pseudo_dice,
            'current_epoch': self.current_epoch,
            'init_args': self.my_init_kwargs,
            'trainer_name': self.__class__.__name__,
        }
        torch.save(checkpoint, filename)

    def load_checkpoint(self, filename_or_checkpoint: Union[dict, str]) -> None:
        if isinstance(filename_or_checkpoint, str):
            checkpoint = torch.load(filename_or_checkpoint, map_location=torch.device('cpu'))
        # if state dict comes from nn.DataParallel but we use non-parallel model here then the state dict keys do not
        # match. Use heuristic to make it match
        new_state_dict = {}
        for k, value in checkpoint['network_weights'].items():
            key = k
            if key not in self.network.state_dict().keys() and key.startswith('module.'):
                key = key[7:]
            new_state_dict[key] = value

        self.my_init_kwargs = checkpoint['init_args']
        self.current_epoch = checkpoint['current_epoch']
        self.my_fantastic_logging = checkpoint['logging']
        self._best_ema = checkpoint['_best_ema']
        self._ema_pseudo_dice = checkpoint['_current_ema']
        self.network.load_state_dict(new_state_dict)
        self.optimizer.load_state_dict(checkpoint['optimizer_state'])
        self.grad_scaler.load_state_dict(checkpoint['grad_scaler_state'])

    def perform_actual_validation(self):
        pass

    def run_training(self):
        self.on_train_start()

        for epoch in range(self.current_epoch, self.num_epochs):
            self.on_epoch_start()

            self.on_train_epoch_start()
            train_outputs = []
            for batch_id in range(self.num_iterations_per_epoch):
                train_outputs.append(self.train_step(next(self.dataloader_train)))
            self.on_train_epoch_end(train_outputs)

            with torch.no_grad():
                self.on_validation_epoch_start()
                val_outputs = []
                for batch_id in range(self.num_val_iterations_per_epoch):
                    val_outputs.append(self.validation_step(next(self.dataloader_val)))
                self.on_validation_epoch_end(val_outputs)

            self.on_epoch_end()

        self.on_train_end()

    def _plot_progress_png(self):
        sns.set(font_scale=2.5)
        fig, ax_all = plt.subplots(3, 1, figsize=(20, 18 * 2))
        # regular progress.png as we are used to from previous nnU-Net versions
        ax = ax_all[0]
        ax2 = ax.twinx()
        x_values = list(range(self.current_epoch + 1))
        ax.plot(x_values, self.my_fantastic_logging['train_losses'], color='b', ls='-', label="loss_tr", linewidth=4)
        ax.plot(x_values, self.my_fantastic_logging['val_losses'], color='r', ls='-', label="loss_val", linewidth=4)
        ax2.plot(x_values, self.my_fantastic_logging['mean_fg_dice'], color='g', ls='--', label="pseudo dice", linewidth=4)
        ax.set_xlabel("epoch")
        ax.set_ylabel("loss")
        ax2.set_ylabel("pseudo dice")
        ax.legend(loc=(0, 1))
        ax2.legend(loc=(0.2, 1))

        # epoch times to see whether the training speed is consistent
        ax = ax_all[1]
        x_values = list(range(self.current_epoch + 1))
        ax.plot(x_values, [i - j for i, j in zip(self.my_fantastic_logging['epoch_end_timestamps'],
                                                 self.my_fantastic_logging['epoch_start_timestamps'])], color='b',
                ls='-', label="epoch duration", linewidth=4)
        ylim = [0] + [ax.get_ylim()[1]]
        ax.set(ylim=ylim)
        ax.set_xlabel("epoch")
        ax.set_ylabel("time [s]")
        ax.legend(loc=(0, 1))

        # learning rate
        ax = ax_all[2]
        x_values = list(range(self.current_epoch + 1))
        ax.plot(x_values, self.my_fantastic_logging['lrs'], color='b', ls='-', label="learning rate", linewidth=4)
        ax.set_xlabel("epoch")
        ax.set_ylabel("learning rate")
        ax.legend(loc=(0, 1))

        plt.tight_layout()

        fig.savefig(join(self.output_folder, "progress.png"))
        plt.close()