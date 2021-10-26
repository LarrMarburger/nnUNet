import argparse

import meddec
from batchgenerators.utilities.file_and_folder_operations import *
from nnunet.run.default_configuration import get_default_configuration
from nnunet.paths import default_plans_identifier
from nnunet.training.cascade_stuff.predict_next_stage import predict_next_stage
from nnunet.training.network_training.nnUNetTrainer import nnUNetTrainer
from nnunet.training.network_training.nnUNetTrainerCascadeFullRes import nnUNetTrainerCascadeFullRes
from nnunet.training.network_training.nnUNetTrainerV2_CascadeFullRes import nnUNetTrainerV2CascadeFullRes

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("network")
    parser.add_argument("network_trainer")
    parser.add_argument("task")
    parser.add_argument("fold", help='0, 1, ..., 5 or \'all\'')
    parser.add_argument("-val", "--validation_only", help="use this if you want to only run the validation",
                        action="store_true")
    parser.add_argument("-c", "--continue_training", help="use this if you want to continue a training",
                        action="store_true")
    parser.add_argument("-p", help="plans identifier", default=default_plans_identifier, required=False)
    parser.add_argument("-u", "--unpack_data", help="Leave it as 1, development only", required=False, default=1,
                        type=int)
    parser.add_argument("--ndet", help="Per default training is deterministic, "
                                                   "nondeterministic allows cudnn.benchmark which will can give up to "
                                                   "20\% performance. Set this to do nondeterministic training",
                        required=False, default=False, action="store_true")
    parser.add_argument("--local_rank", default=0, type=int)
    parser.add_argument("--fp16", required=False, default=False, action="store_true")
    parser.add_argument("--dbs", required=False, default=False, action="store_true", help="distribute batch size. If "
                                                                                          "True then whatever "
                                                                                          "batch_size is in plans will "
                                                                                          "be distributed over DDP "
                                                                                          "models, if False then each "
                                                                                          "model will have batch_size "
                                                                                          "for a total of "
                                                                                          "GPUs*batch_size")
    parser.add_argument("--npz", required=False, default=False, action="store_true", help="if set then nnUNet will "
                                                                                          "export npz files of "
                                                                                          "predicted segmentations "
                                                                                          "in the vlaidation as well. "
                                                                                          "This is needed to run the "
                                                                                          "ensembling step so unless "
                                                                                          "you are developing nnUNet "
                                                                                          "you should enable this")
    parser.add_argument("--valbest", required=False, default=False, action="store_true", help="")
    parser.add_argument("--find_lr", required=False, default=False, action="store_true", help="")

    args = parser.parse_args()

    task = args.task
    fold = args.fold
    network = args.network
    network_trainer = args.network_trainer
    validation_only = args.validation_only
    plans_identifier = args.p
    unpack = args.unpack_data
    deterministic = not args.ndet
    valbest = args.valbest
    find_lr = args.find_lr

    if unpack == 0:
        unpack = False
    elif unpack == 1:
        unpack = True
    else:
        raise ValueError("Unexpected value for -u/--unpack_data: %s. Use 1 or 0." % str(unpack))

    if fold == 'all':
        pass
    else:
        fold = int(fold)

    plans_file, output_folder_name, dataset_directory, batch_dice, stage, \
        trainer_class = get_default_configuration(network, task, network_trainer, plans_identifier)

    if trainer_class is None:
        raise RuntimeError("Could not find trainer class in meddec.model_training")

    if network == "3d_cascade_fullres":
        assert issubclass(trainer_class, (nnUNetTrainerCascadeFullRes, nnUNetTrainerV2CascadeFullRes)), \
            "If running 3d_cascade_fullres then your " \
            "trainer class must be derived from " \
            "nnUNetTrainerCascadeFullRes"
    else:
        assert issubclass(trainer_class,
                          nnUNetTrainer), "network_trainer was found but is not derived from nnUNetTrainer"

    trainer = trainer_class(plans_file, fold, local_rank=args.local_rank, output_folder=output_folder_name,
                            dataset_directory=dataset_directory, batch_dice=batch_dice, stage=stage,
                            unpack_data=unpack, deterministic=deterministic, fp16=args.fp16,
                            distribute_batch_size=args.dbs)

    trainer.initialize(not validation_only)

    if find_lr:
        trainer.find_lr()
    else:
        if not validation_only:
            if args.continue_training:
                trainer.load_latest_checkpoint()
            trainer.run_training()
        else:
            trainer.load_latest_checkpoint(train=False)

        # predict validation
        trainer.validate(save_softmax=args.npz)

        if network == '3d_lowres':
            trainer.load_best_checkpoint(False)
            print("predicting segmentations for the next stage of the cascade")
            predict_next_stage(trainer, join(dataset_directory, trainer.plans['data_identifier'] + "_stage%d" % 1))
