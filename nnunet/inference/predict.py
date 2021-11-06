#    Copyright 2019 Division of Medical Image Computing, German Cancer Research Center (DKFZ), Heidelberg, Germany
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import argparse
import sys

import numpy as np
from batchgenerators.augmentations.utils import resize_segmentation
from nnunet.experiment_planning.plan_and_preprocess_task import get_caseIDs_from_splitted_dataset_folder
from nnunet.inference.segmentation_export import save_segmentation_nifti_from_softmax, save_segmentation_nifti
from batchgenerators.utilities.file_and_folder_operations import *
from multiprocessing import Process, Queue
import torch
import SimpleITK as sitk
import shutil
from multiprocessing import Pool
from nnunet.postprocessing.connected_components import load_remove_save, load_for_which_classes
from nnunet.training.model_restore import load_model_and_checkpoint_files
from nnunet.training.network_training.nnUNetTrainer import nnUNetTrainer
from nnunet.utilities.one_hot_encoding import to_one_hot


def preprocess_save_to_queue(preprocess_fn, q, list_of_lists, output_files, segs_from_prev_stage, classes):
    # suppress output
    sys.stdout = open(os.devnull, 'w')

    errors_in = []
    for i, l in enumerate(list_of_lists):
        try:
            output_file = output_files[i]
            print("preprocessing", output_file)
            d, _, dct = preprocess_fn(l)
            # print(output_file, dct)
            if segs_from_prev_stage[i] is not None:
                assert isfile(segs_from_prev_stage[i]) and segs_from_prev_stage[i].endswith(
                    ".nii.gz"), "segs_from_prev_stage" \
                                " must point to a " \
                                "segmentation file"
                seg_prev = sitk.GetArrayFromImage(sitk.ReadImage(segs_from_prev_stage[i]))
                # check to see if shapes match
                img = sitk.GetArrayFromImage(sitk.ReadImage(l[0]))
                assert all([i == j for i, j in zip(seg_prev.shape, img.shape)]), "image and segmentation from previous " \
                                                                                 "stage don't have the same pixel array " \
                                                                                 "shape! image: %s, seg_prev: %s" % \
                                                                                 (l[0], segs_from_prev_stage[i])
                seg_reshaped = resize_segmentation(seg_prev, d.shape[1:], order=1, cval=0)
                seg_reshaped = to_one_hot(seg_reshaped, classes)
                d = np.vstack((d, seg_reshaped)).astype(np.float32)
            """There is a problem with python process communication that prevents us from communicating obejcts 
            larger than 2 GB between processes (basically when the length of the pickle string that will be sent is 
            communicated by the multiprocessing.Pipe object then the placeholder (\%i I think) does not allow for long 
            enough strings (lol). This could be fixed by changing i to l (for long) but that would require manually 
            patching system python code. We circumvent that problem here by saving softmax_pred to a npy file that will 
            then be read (and finally deleted) by the Process. save_segmentation_nifti_from_softmax can take either 
            filename or np.ndarray and will handle this automatically"""
            print(d.shape)
            if np.prod(d.shape) > (2e9 / 4 * 0.85):  # *0.85 just to be save, 4 because float32 is 4 bytes
                print(
                    "This output is too large for python process-process communication. "
                    "Saving output temporarily to disk")
                np.save(output_file[:-7] + ".npy", d)
                d = output_file[:-7] + ".npy"
            q.put((output_file, (d, dct)))
        except KeyboardInterrupt:
            raise KeyboardInterrupt
        except Exception as e:
            print("error in", l)
            print(e)
    q.put("end")
    if len(errors_in) > 0:
        print("There were some errors in the following cases:", errors_in)
        print("These cases were ignored.")
    else:
        print("This worker has ended successfully, no errors to report")
    # restore output
    sys.stdout = sys.__stdout__


def preprocess_multithreaded(trainer, list_of_lists, output_files, num_processes=2, segs_from_prev_stage=None):
    if segs_from_prev_stage is None:
        segs_from_prev_stage = [None] * len(list_of_lists)

    classes = list(range(1, trainer.num_classes))
    assert isinstance(trainer, nnUNetTrainer)
    q = Queue(2)
    processes = []
    for i in range(num_processes):
        pr = Process(target=preprocess_save_to_queue, args=(trainer.preprocess_patient, q,
                                                         list_of_lists[i::num_processes],
                                                         output_files[i::num_processes],
                                                         segs_from_prev_stage[i::num_processes],
                                                            classes))
        pr.start()
        processes.append(pr)

    try:
        end_ctr = 0
        while end_ctr != num_processes:
            item = q.get()
            if item == "end":
                end_ctr += 1
                continue
            else:
                yield item

    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()  # this should not happen but better safe than sorry right
            p.join()

        q.close()


def predict_cases(model, list_of_lists, output_filenames, folds, save_npz, num_threads_preprocessing,
                  num_threads_nifti_save, segs_from_prev_stage=None, do_tta=True, fp16=None, overwrite_existing=False,
                  all_in_gpu=False, step=2, force_separate_z=None, interp_order=1):
    """

    :param model:
    :param list_of_lists:
    :param output_filenames:
    :param folds:
    :param save_npz:
    :param num_threads_preprocessing:
    :param num_threads_nifti_save:
    :param segs_from_prev_stage:
    :param do_tta:
    :param overwrite_existing:
    :param fp16: if None then we take no action. If True/False we overwrite what the model has in its init
    :return:
    """
    assert len(list_of_lists) == len(output_filenames)
    if segs_from_prev_stage is not None: assert len(segs_from_prev_stage) == len(output_filenames)

    pool = Pool(num_threads_nifti_save)
    results = []

    cleaned_output_files = []
    for o in output_filenames:
        dr, f = os.path.split(o)
        if len(dr) > 0:
            maybe_mkdir_p(dr)
        if not f.endswith(".nii.gz"):
            f, _ = os.path.splitext(f)
            f = f + ".nii.gz"
        cleaned_output_files.append(join(dr, f))

    if not overwrite_existing:
        print("number of cases:", len(list_of_lists))
        not_done_idx = [i for i, j in enumerate(cleaned_output_files) if not isfile(j)]

        cleaned_output_files = [cleaned_output_files[i] for i in not_done_idx]
        list_of_lists = [list_of_lists[i] for i in not_done_idx]
        if segs_from_prev_stage is not None:
            segs_from_prev_stage = [segs_from_prev_stage[i] for i in not_done_idx]

        print("number of cases that still need to be predicted:", len(cleaned_output_files))

    print("emptying cuda cache")
    torch.cuda.empty_cache()

    print("loading parameters for folds,", folds)
    trainer, params = load_model_and_checkpoint_files(model, folds, fp16=fp16)

    print("starting preprocessing generator")
    preprocessing = preprocess_multithreaded(trainer, list_of_lists, cleaned_output_files, num_threads_preprocessing,
                                             segs_from_prev_stage)
    print("starting prediction...")
    all_output_files = []
    for preprocessed in preprocessing:
        output_filename, (d, dct) = preprocessed
        all_output_files.append(all_output_files)
        if isinstance(d, str):
            data = np.load(d)
            os.remove(d)
            d = data

        print("predicting", output_filename)

        softmax = []
        for p in params:
            trainer.load_checkpoint_ram(p, False)
            softmax.append(trainer.predict_preprocessed_data_return_softmax(d, do_tta, 1, False, 1,
                                                                            trainer.data_aug_params['mirror_axes'],
                                                                            True, True, step, trainer.patch_size, True,
                                                                            all_in_gpu=all_in_gpu)[None])

        softmax = np.vstack(softmax)
        softmax_mean = np.mean(softmax, 0)

        transpose_forward = trainer.plans.get('transpose_forward')
        if transpose_forward is not None:
            transpose_backward = trainer.plans.get('transpose_backward')
            softmax_mean = softmax_mean.transpose([0] + [i + 1 for i in transpose_backward])

        if save_npz:
            npz_file = output_filename[:-7] + ".npz"
        else:
            npz_file = None

        """There is a problem with python process communication that prevents us from communicating obejcts 
        larger than 2 GB between processes (basically when the length of the pickle string that will be sent is 
        communicated by the multiprocessing.Pipe object then the placeholder (\%i I think) does not allow for long 
        enough strings (lol). This could be fixed by changing i to l (for long) but that would require manually 
        patching system python code. We circumvent that problem here by saving softmax_pred to a npy file that will 
        then be read (and finally deleted) by the Process. save_segmentation_nifti_from_softmax can take either 
        filename or np.ndarray and will handle this automatically"""
        bytes_per_voxel = 4
        if all_in_gpu:
            bytes_per_voxel = 2 # if all_in_gpu then the return value is half (float16)
        if np.prod(softmax_mean.shape) > (2e9 / bytes_per_voxel * 0.85):  # * 0.85 just to be save
            print(
                "This output is too large for python process-process communication. Saving output temporarily to disk")
            np.save(output_filename[:-7] + ".npy", softmax_mean)
            softmax_mean = output_filename[:-7] + ".npy"

        results.append(pool.starmap_async(save_segmentation_nifti_from_softmax,
                                          ((softmax_mean, output_filename, dct, interp_order, None, None, None,
                                            npz_file, None, force_separate_z),)
                                          ))

    print("inference done. Now waiting for the segmentation export to finish...")
    _ = [i.get() for i in results]
    # now apply postprocessing
    # first load the postprocessing properties if they are present. Else raise a well visible warning
    results = []
    pp_file = join(model, "postprocessing.json")
    if isfile(pp_file):
        print("postprocessing...")
        shutil.copy(pp_file, os.path.dirname(output_filenames[0]))
        # for_which_classes stores for which of the classes everything but the largest connected component needs to be
        # removed
        for_which_classes = load_for_which_classes(pp_file)
        results.append(pool.starmap_async(load_remove_save,
                                          zip(output_filenames, output_filenames,
                                              [for_which_classes] * len(output_filenames))))
        _ = [i.get() for i in results]
    else:
        print("WARNING! Cannot run postprocessing because the postprocessing file is missing. Make sure to run "
              "consolidate_folds in the output folder of the model first!\nThe folder you need to run this in is "
              "%s" % model)

    pool.close()
    pool.join()


def predict_cases_fast(model, list_of_lists, output_filenames, folds, num_threads_preprocessing,
                       num_threads_nifti_save, segs_from_prev_stage=None, do_tta=True, fp16=None,
                       overwrite_existing=False, all_in_gpu=True, step=2, checkpoint_name="model_best",
                       force_separate_z=None, interp_order=1):
    assert len(list_of_lists) == len(output_filenames)
    if segs_from_prev_stage is not None: assert len(segs_from_prev_stage) == len(output_filenames)

    pool = Pool(num_threads_nifti_save)
    results = []

    cleaned_output_files = []
    for o in output_filenames:
        dr, f = os.path.split(o)
        if len(dr) > 0:
            maybe_mkdir_p(dr)
        if not f.endswith(".nii.gz"):
            f, _ = os.path.splitext(f)
            f = f + ".nii.gz"
        cleaned_output_files.append(join(dr, f))

    if not overwrite_existing:
        print("number of cases:", len(list_of_lists))
        not_done_idx = [i for i, j in enumerate(cleaned_output_files) if not isfile(j)]

        cleaned_output_files = [cleaned_output_files[i] for i in not_done_idx]
        list_of_lists = [list_of_lists[i] for i in not_done_idx]
        if segs_from_prev_stage is not None:
            segs_from_prev_stage = [segs_from_prev_stage[i] for i in not_done_idx]

        print("number of cases that still need to be predicted:", len(cleaned_output_files))

    print("emptying cuda cache")
    torch.cuda.empty_cache()

    print("loading parameters for folds,", folds)
    trainer, params = load_model_and_checkpoint_files(model, folds, fp16=fp16, checkpoint_name=checkpoint_name)

    print("starting preprocessing generator")
    preprocessing = preprocess_multithreaded(trainer, list_of_lists, cleaned_output_files, num_threads_preprocessing,
                                             segs_from_prev_stage)

    print("starting prediction...")
    for preprocessed in preprocessing:
        print("getting data from preprocessor")
        output_filename, (d, dct) = preprocessed
        print("got something")
        if isinstance(d, str):
            print("what I got is a string, so I need to load a file")
            data = np.load(d)
            os.remove(d)
            d = data

        # preallocate the output arrays
        # same dtype as the return value in predict_preprocessed_data_return_softmax_and_seg (saves time)
        softmax_aggr = None # np.zeros((trainer.num_classes, *d.shape[1:]), dtype=np.float16)
        all_seg_outputs = np.zeros((len(params), *d.shape[1:]), dtype=int)
        print("predicting", output_filename)

        for i, p in enumerate(params):
            trainer.load_checkpoint_ram(p, False)
            res = trainer.predict_preprocessed_data_return_softmax_and_seg(d, do_tta, 1, False, 1,
                                                                           trainer.data_aug_params['mirror_axes'],
                                                                           True, True, step, trainer.patch_size, True,
                                                                           all_in_gpu=all_in_gpu)
            if len(params) > 1:
                # otherwise we dont need this and we can save ourselves the time it takes to copy that
                print("aggregating softmax")
                if softmax_aggr is None:
                    softmax_aggr = res[1]
                else:
                    softmax_aggr += res[1]
            all_seg_outputs[i] = res[0]

        print("obtaining segmentation map")
        if len(params) > 1:
            # we dont need to normalize the softmax by 1 / len(params) because this would not change the outcome of the argmax
            seg = softmax_aggr.argmax(0)
        else:
            seg = all_seg_outputs[0]

        print("applying transpose_backward")
        transpose_forward = trainer.plans.get('transpose_forward')
        if transpose_forward is not None:
            transpose_backward = trainer.plans.get('transpose_backward')
            seg = seg.transpose([i for i in transpose_backward])

        print("initializing segmentation export")
        results.append(pool.starmap_async(save_segmentation_nifti,
                                           ((seg, output_filename, dct, interp_order, force_separate_z),)
                                           ))
        print("done")

    print("inference done. Now waiting for the segmentation export to finish...")
    _ = [i.get() for i in results]
    # now apply postprocessing
    # first load the postprocessing properties if they are present. Else raise a well visible warning
    results = []
    pp_file = join(model, "postprocessing.json")
    if isfile(pp_file):
        print("postprocessing...")
        shutil.copy(pp_file, os.path.dirname(output_filenames[0]))
        # for_which_classes stores for which of the classes everything but the largest connected component needs to be
        # removed
        for_which_classes = load_for_which_classes(pp_file)
        results.append(pool.starmap_async(load_remove_save,
                                          zip(output_filenames, output_filenames,
                                              [for_which_classes] * len(output_filenames))))
        _ = [i.get() for i in results]
    else:
        print("WARNING! Cannot run postprocessing because the postprocessing file is missing. Make sure to run "
              "consolidate_folds in the output folder of the model first!\nThe folder you need to run this in is "
              "%s" % model)

    pool.close()
    pool.join()


def predict_cases_fastest(model, list_of_lists, output_filenames, folds, num_threads_preprocessing,
                          num_threads_nifti_save, segs_from_prev_stage=None, do_tta=True, fp16=None,
                          overwrite_existing=False, all_in_gpu=True, step=2):
    assert len(list_of_lists) == len(output_filenames)
    if segs_from_prev_stage is not None: assert len(segs_from_prev_stage) == len(output_filenames)

    pool = Pool(num_threads_nifti_save)
    results = []

    cleaned_output_files = []
    for o in output_filenames:
        dr, f = os.path.split(o)
        if len(dr) > 0:
            maybe_mkdir_p(dr)
        if not f.endswith(".nii.gz"):
            f, _ = os.path.splitext(f)
            f = f + ".nii.gz"
        cleaned_output_files.append(join(dr, f))

    if not overwrite_existing:
        print("number of cases:", len(list_of_lists))
        not_done_idx = [i for i, j in enumerate(cleaned_output_files) if not isfile(j)]

        cleaned_output_files = [cleaned_output_files[i] for i in not_done_idx]
        list_of_lists = [list_of_lists[i] for i in not_done_idx]
        if segs_from_prev_stage is not None:
            segs_from_prev_stage = [segs_from_prev_stage[i] for i in not_done_idx]

        print("number of cases that still need to be predicted:", len(cleaned_output_files))

    print("emptying cuda cache")
    torch.cuda.empty_cache()

    print("loading parameters for folds,", folds)
    trainer, params = load_model_and_checkpoint_files(model, folds, fp16=fp16)

    print("starting preprocessing generator")
    preprocessing = preprocess_multithreaded(trainer, list_of_lists, cleaned_output_files, num_threads_preprocessing,
                                             segs_from_prev_stage)

    print("starting prediction...")
    for preprocessed in preprocessing:
        print("getting data from preprocessor")
        output_filename, (d, dct) = preprocessed
        print("got something")
        if isinstance(d, str):
            print("what I got is a string, so I need to load a file")
            data = np.load(d)
            os.remove(d)
            d = data

        # preallocate the output arrays
        # same dtype as the return value in predict_preprocessed_data_return_softmax_and_seg (saves time)
        all_softmax_outputs = np.zeros((len(params), trainer.num_classes, *d.shape[1:]), dtype=np.float16)
        all_seg_outputs = np.zeros((len(params), *d.shape[1:]), dtype=int)
        print("predicting", output_filename)

        for i, p in enumerate(params):
            trainer.load_checkpoint_ram(p, False)
            res = trainer.predict_preprocessed_data_return_softmax_and_seg(d, do_tta, 1, False, 1,
                                                                           trainer.data_aug_params['mirror_axes'],
                                                                           True, True, step, trainer.patch_size, True,
                                                                           all_in_gpu=all_in_gpu)
            if len(params) > 1:
                # otherwise we dont need this and we can save ourselves the time it takes to copy that
                all_softmax_outputs[i] = res[1]
            all_seg_outputs[i] = res[0]

        print("aggregating predictions")
        if len(params) > 1:
            softmax_mean = np.mean(all_softmax_outputs, 0)
            seg = softmax_mean.argmax(0)
        else:
            seg = all_seg_outputs[0]

        print("applying transpose_backward")
        transpose_forward = trainer.plans.get('transpose_forward')
        if transpose_forward is not None:
            transpose_backward = trainer.plans.get('transpose_backward')
            seg = seg.transpose([i for i in transpose_backward])

        print("initializing segmentation export")
        results.append(pool.starmap_async(save_segmentation_nifti,
                                           ((seg, output_filename, dct, 0, None),)
                                           ))
        print("done")

    print("inference done. Now waiting for the segmentation export to finish...")
    _ = [i.get() for i in results]
    # now apply postprocessing
    # first load the postprocessing properties if they are present. Else raise a well visible warning
    results = []
    pp_file = join(model, "postprocessing.json")
    if isfile(pp_file):
        print("postprocessing...")
        shutil.copy(pp_file, os.path.dirname(output_filenames[0]))
        # for_which_classes stores for which of the classes everything but the largest connected component needs to be
        # removed
        for_which_classes = load_for_which_classes(pp_file)
        results.append(pool.starmap_async(load_remove_save,
                                          zip(output_filenames, output_filenames,
                                              [for_which_classes] * len(output_filenames))))
        _ = [i.get() for i in results]
    else:
        print("WARNING! Cannot run postprocessing because the postprocessing file is missing. Make sure to run "
              "consolidate_folds in the output folder of the model first!\nThe folder you need to run this in is "
              "%s" % model)

    pool.close()
    pool.join()


def predict_from_folder(model, input_folder, output_folder, folds, save_npz, num_threads_preprocessing,
                        num_threads_nifti_save, lowres_segmentations, part_id, num_parts, tta, fp16=False,
                        overwrite_existing=True, mode='normal', overwrite_all_in_gpu=None, step=2,
                        force_separate_z=None, interp_order=1):
    """
        here we use the standard naming scheme to generate list_of_lists and output_files needed by predict_cases

    :param model:
    :param input_folder:
    :param output_folder:
    :param folds:
    :param save_npz:
    :param num_threads_preprocessing:
    :param num_threads_nifti_save:
    :param lowres_segmentations:
    :param part_id:
    :param num_parts:
    :param tta:
    :param fp16:
    :param overwrite_existing: if not None then it will be overwritten with whatever is in there. None is default (no overwrite)
    :return:
    """
    maybe_mkdir_p(output_folder)
    shutil.copy(join(model, 'plans.pkl'), output_folder)

    case_ids = get_caseIDs_from_splitted_dataset_folder(input_folder)
    output_files = [join(output_folder, i + ".nii.gz") for i in case_ids]
    all_files = subfiles(input_folder, suffix=".nii.gz", join=False, sort=True)
    list_of_lists = [[join(input_folder, i) for i in all_files if i[:len(j)].startswith(j) and
                      len(i) == (len(j) + 12)] for j in case_ids]

    if lowres_segmentations is not None:
        assert isdir(lowres_segmentations), "if lowres_segmentations is not None then it must point to a directory"
        lowres_segmentations = [join(lowres_segmentations, i + ".nii.gz") for i in case_ids]
        assert all([isfile(i) for i in lowres_segmentations]), "not all lowres_segmentations files are present. " \
                                                               "(I was searching for case_id.nii.gz in that folder)"
        lowres_segmentations = lowres_segmentations[part_id::num_parts]
    else:
        lowres_segmentations = None

    if mode == "normal":
        if overwrite_all_in_gpu is None:
            all_in_gpu = False
        else:
            all_in_gpu = overwrite_all_in_gpu

        return predict_cases(model, list_of_lists[part_id::num_parts], output_files[part_id::num_parts], folds,
                             save_npz,
                             num_threads_preprocessing, num_threads_nifti_save, lowres_segmentations,
                             tta, fp16=fp16, overwrite_existing=overwrite_existing, all_in_gpu=all_in_gpu, step=step,
                             force_separate_z=force_separate_z, interp_order=interp_order)
    elif mode == "fast":
        if overwrite_all_in_gpu is None:
            all_in_gpu = True
        else:
            all_in_gpu = overwrite_all_in_gpu

        assert save_npz is False
        return predict_cases_fast(model, list_of_lists[part_id::num_parts], output_files[part_id::num_parts], folds,
                                  num_threads_preprocessing, num_threads_nifti_save, lowres_segmentations,
                                  tta, fp16=fp16, overwrite_existing=overwrite_existing, all_in_gpu=all_in_gpu, step=step,
                                  force_separate_z=force_separate_z, interp_order=interp_order)
    elif mode == "fastest":
        if overwrite_all_in_gpu is None:
            all_in_gpu = True
        else:
            all_in_gpu = overwrite_all_in_gpu

        assert save_npz is False
        return predict_cases_fastest(model, list_of_lists[part_id::num_parts], output_files[part_id::num_parts], folds,
                                     num_threads_preprocessing, num_threads_nifti_save, lowres_segmentations,
                                     tta, fp16=fp16, overwrite_existing=overwrite_existing, all_in_gpu=all_in_gpu, step=step)
    else:
        raise ValueError("unrecognized mode. Must be normal, fast or fastest")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", '--input_folder', help="Must contain all modalities for each patient in the correct"
                                                     " order (same as training). Files must be named "
                                                     "CASENAME_XXXX.nii.gz where XXXX is the modality "
                                                     "identifier (0000, 0001, etc)", required=True)
    parser.add_argument('-o', "--output_folder", required=True, help="folder for saving predictions")
    parser.add_argument('-m', '--model_output_folder',
                        help='model output folder. Will automatically discover the folds '
                             'that were '
                             'run and use those as an ensemble', required=True)
    parser.add_argument('-f', '--folds', nargs='+', default='None', help="folds to use for prediction. Default is None "
                                                                         "which means that folds will be detected "
                                                                         "automatically in the model output folder")
    parser.add_argument('-z', '--save_npz', required=False, action='store_true', help="use this if you want to ensemble"
                                                                                      " these predictions with those of"
                                                                                      " other models. Softmax "
                                                                                      "probabilities will be saved as "
                                                                                      "compresed numpy arrays in "
                                                                                      "output_folder and can be merged "
                                                                                      "between output_folders with "
                                                                                      "merge_predictions.py")
    parser.add_argument('-l', '--lowres_segmentations', required=False, default='None', help="if model is the highres "
                                                                                             "stage of the cascade then you need to use -l to specify where the segmentations of the "
                                                                                             "corresponding lowres unet are. Here they are required to do a prediction")
    parser.add_argument("--part_id", type=int, required=False, default=0, help="Used to parallelize the prediction of "
                                                                               "the folder over several GPUs. If you "
                                                                               "want to use n GPUs to predict this "
                                                                               "folder you need to run this command "
                                                                               "n times with --part_id=0, ... n-1 and "
                                                                               "--num_parts=n (each with a different "
                                                                               "GPU (for example via "
                                                                               "CUDA_VISIBLE_DEVICES=X)")
    parser.add_argument("--num_parts", type=int, required=False, default=1,
                        help="Used to parallelize the prediction of "
                             "the folder over several GPUs. If you "
                             "want to use n GPUs to predict this "
                             "folder you need to run this command "
                             "n times with --part_id=0, ... n-1 and "
                             "--num_parts=n (each with a different "
                             "GPU (via "
                             "CUDA_VISIBLE_DEVICES=X)")
    parser.add_argument("--num_threads_preprocessing", required=False, default=6, type=int, help=
    "Determines many background processes will be used for data preprocessing. Reduce this if you "
    "run into out of memory (RAM) problems. Default: 6")
    parser.add_argument("--num_threads_nifti_save", required=False, default=2, type=int, help=
    "Determines many background processes will be used for segmentation export. Reduce this if you "
    "run into out of memory (RAM) problems. Default: 2")
    parser.add_argument("--tta", required=False, type=int, default=1, help="Set to 0 to disable test time data "
                                                                           "augmentation (speedup of factor "
                                                                           "4(2D)/8(3D)), "
                                                                           "lower quality segmentations")
    parser.add_argument("--fp16", required=False, help="Flag for inference in FP16, default = off. DO NOT USE! It "
                                                       "doesn't work", action="store_true")
    parser.add_argument("--overwrite_existing", required=False, type=int, default=1, help="Set this to 0 if you need "
                                                                                          "to resume a previous "
                                                                                          "prediction. Default: 1 "
                                                                                          "(=existing segmentations "
                                                                                          "in output_folder will be "
                                                                                          "overwritten)")
    parser.add_argument("--mode", type=str, default="normal", required=False)
    parser.add_argument("--all_in_gpu", type=str, default="None", required=False, help="can be None, False or True")
    parser.add_argument("--step", type=float, default=2, required=False, help="don't touch")
    parser.add_argument("--interp_order", required=False, default=3, type=int,
                        help="order of interpolation for segmentations, has no effect if mode=fastest")
    parser.add_argument("--force_separate_z", required=False, default="None", type=str,
                        help="force_separate_z resampling. Can be None, True or False, has no effect if mode=fastest")

    args = parser.parse_args()
    input_folder = args.input_folder
    output_folder = args.output_folder
    part_id = args.part_id
    num_parts = args.num_parts
    model = args.model_output_folder
    folds = args.folds
    save_npz = args.save_npz
    lowres_segmentations = args.lowres_segmentations
    num_threads_preprocessing = args.num_threads_preprocessing
    num_threads_nifti_save = args.num_threads_nifti_save
    tta = args.tta
    fp16 = args.fp16
    step = args.step

    interp_order = args.interp_order
    force_separate_z = args.force_separate_z

    if force_separate_z == "None":
        force_separate_z = None
    elif force_separate_z == "False":
        force_separate_z = False
    elif force_separate_z == "True":
        force_separate_z = True
    else:
        raise ValueError("force_separate_z must be None, True or False. Given: %s" % force_separate_z)

    if fp16:
        raise RuntimeError("FP16 support for inference does not work yet. Sorry :-/")

    overwrite = args.overwrite_existing
    mode = args.mode
    all_in_gpu = args.all_in_gpu

    if lowres_segmentations == "None":
        lowres_segmentations = None

    if isinstance(folds, list):
        if folds[0] == 'all' and len(folds) == 1:
            pass
        else:
            folds = [int(i) for i in folds]
    elif folds == "None":
        folds = None
    else:
        raise ValueError("Unexpected value for argument folds")

    if tta == 0:
        tta = False
    elif tta == 1:
        tta = True
    else:
        raise ValueError("Unexpected value for tta, Use 1 or 0")

    if overwrite == 0:
        overwrite = False
    elif overwrite == 1:
        overwrite = True
    else:
        raise ValueError("Unexpected value for overwrite, Use 1 or 0")

    assert all_in_gpu in ['None', 'False', 'True']
    if all_in_gpu == "None":
        all_in_gpu = None
    elif all_in_gpu == "True":
        all_in_gpu = True
    elif all_in_gpu == "False":
        all_in_gpu = False

    predict_from_folder(model, input_folder, output_folder, folds, save_npz, num_threads_preprocessing,
                        num_threads_nifti_save, lowres_segmentations, part_id, num_parts, tta, fp16=fp16,
                        overwrite_existing=overwrite, mode=mode, overwrite_all_in_gpu=all_in_gpu, step=step,
                        force_separate_z=force_separate_z, interp_order=interp_order)
