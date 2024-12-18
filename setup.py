from setuptools import setup, find_namespace_packages

setup(name='nnunetv2',
      packages=find_namespace_packages(include=["nnunetv2", "nnunetv2.*"]),
      version='2',
      description='nnU-Net. Framework for out-of-the box biomedical image segmentation.',
      url='https://github.com/MIC-DKFZ/nnUNet',
      author='Helmholtz Imaging Applied Computer Vision Lab, Division of Medical Image Computing, German Cancer Research Center',
      author_email='f.isensee@dkfz-heidelberg.de',
      license='Apache License Version 2.0, January 2004',
      install_requires=[
          "torch>=1.8.0a",
          "tqdm",
          "dicom2nifti",
          "scikit-image>=0.14",
          "medpy",
          "scipy",
          "batchgenerators>=0.22",
          "numpy",
          "scikit-learn",
          "SimpleITK",
          "pandas",
          "graphviz",
          'tifffile',
          'requests',
          "nibabel",
          "matplotlib",
          "seaborn",
          "imagecodecs",
          "yacs"
      ],
      entry_points={
          'console_scripts': [
              'nnUNetv2_plan_and_preprocess = nnunetv2.experiment_planning.plan_and_preprocess:plan_and_preprocess',
              'nnUNetv2_extract_fingerprint = nnunetv2.experiment_planning.plan_and_preprocess:extract_fingerprint',
              'nnUNetv2_plan_experiment = nnunetv2.experiment_planning.plan_and_preprocess:plan_experiment',
              'nnUNetv2_preprocess = nnunetv2.experiment_planning.plan_and_preprocess:preprocess',
              'nnUNetv2_train = nnunetv2.run.train_nolightning:nnUNet_train_from_args',
              'nnUNetv2_predict_from_modelfolder = nnunetv2.inference.predict_from_raw_data:predict_entry_point_modelfolder',
              'nnUNetv2_predict = nnunetv2.inference.predict_from_raw_data:predict_entry_point',
              'nnUNetv2_convert_old_nnUNet_dataset = nnunetv2.dataset_conversion.convert_raw_dataset_from_old_nnunet_format:convert_entry_point',
              'nnUNetv2_find_best_configuration = nnunetv2.evaluation.find_best_configuration:find_best_configuration_entry_point',
              'nnUNetv2_determine_postprocessing = nnunetv2.postprocessing.remove_connected_components:entry_point_determine_postprocessing_folder',
              'nnUNetv2_apply_postprocessing = nnunetv2.postprocessing.remove_connected_components:entry_point_apply_postprocessing',
              'nnUNetv2_ensemble = nnunetv2.ensembling.ensemble:entry_point_ensemble_folders',
              'nnUNetv2_accumulate_crossval_results = nnunetv2.evaluation.find_best_configuration:accumulate_crossval_results_entry_point',
              'nnUNetv2_plot_overlay_pngs = nnunetv2.utilities.overlay_plots:entry_point_generate_overlay',
              'nnUNetv2_download_pretrained_model_by_url = nnunetv2.model_sharing.entry_points:download_by_url',
              'nnUNetv2_install_pretrained_model_from_zip = nnunetv2.model_sharing.entry_points:install_from_zip_entry_point',
              'nnUNetv2_move_plans_between_datasets = nnunetv2.experiment_planning.plans_for_pretraining.move_plans_between_datasets:entry_point_move_plans_between_datasets',
              'nnUNetv2_evaluate_folder = nnunetv2.evaluation.evaluate_predictions:evaluate_folder_entry_point',
              'nnUNetv2_evaluate_simple = nnunetv2.evaluation.evaluate_predictions:evaluate_simple_entry_point',
              'nnUNetv2_convert_MSD_dataset = nnunetv2.dataset_conversion.convert_MSD_dataset:entry_point'
          ],
      },
      keywords=['deep learning', 'image segmentation', 'medical image analysis',
                'medical image segmentation', 'nnU-Net', 'nnunet']
      )
