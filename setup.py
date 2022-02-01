from setuptools import setup

setup(name='nnunet',
      packages=["nnunet",
                "nnunet.dataset_conversion",
                "nnunet.evaluation",
                "nnunet.evaluation.model_selection",
                "nnunet.experiment_planning",
                "nnunet.inference",
                "nnunet.network_architecture",
                "nnunet.preprocessing",
                "nnunet.run",
                "nnunet.training",
                "nnunet.training.cascade_stuff",
                "nnunet.training.data_augmentation",
                "nnunet.training.dataloading",
                "nnunet.training.loss_functions",
                "nnunet.training.network_training",
                "nnunet.training.network_training.nnUNet_variants",
                "nnunet.utilities",
                ],
      version='0.6',
      description='no new-net. Framework for out-of-the box medical image segmentation.',
      url='https://github.com/MIC-DKFZ/nnUNet',
      author='Division of Medical Image Computing, German Cancer Research Center',
      author_email='f.isensee@dkfz-heidelberg.de',
      license='Apache License Version 2.0, January 2004',
      install_requires=[
            "torch",
            "tqdm",
            "dicom2nifti",
            "scikit-image>=0.14",
            "medpy",
            "scipy",
            "batchgenerators>=0.19.7",
            "numpy",
            "sklearn",
            "SimpleITK",
            "pandas",
            "pandas",
            "apex>=0.1",
            "nibabel"
      ],
      entry_points={
          'console_scripts': [
              'nnUNet_convert_decathlon_task = nnunet.experiment_planning.nnUNet_convert_decathlon_task:main',
              'nnUNet_plan_and_preprocess = nnunet.experiment_planning.nnUNet_plan_and_preprocess:main',
              'nnUNet_train = nnunet.run.run_training:main',
              'nnUNet_train_DP = nnunet.run.run_training_DP:main',
              'nnUNet_train_DDP = nnunet.run.run_training_DDP:main',
              'nnUNet_predict = nnunet.inference.predict_simple:main',
          ],
      },
      keywords=['deep learning', 'image segmentation', 'medical image analysis',
                'medical image segmentation', 'nnU-Net', 'nnunet']
      )
