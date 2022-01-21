# Copyright 2021 solo-learn development team.

# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
# Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies
# or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
import os
from pathlib import Path
from typing import Any, Callable, List, Type

import numpy as np
import torch
from ffcv.fields.decoders import IntDecoder, RandomResizedCropRGBImageDecoder
from ffcv.loader import Loader, OrderOption
from ffcv.transforms import (
    NormalizeImage,
    RandomHorizontalFlip,
    Squeeze,
    ToDevice,
    ToTensor,
    ToTorchImage,
)
from PIL import Image
from solo.utils.pretrain_dataloader import GaussianBlur, Solarization
from torch.utils.data.dataset import Dataset
from torchvision import transforms


def dataset_with_index(DatasetClass: Type[Loader]) -> Type[Dataset]:
    """Factory for datasets that also returns the data index.

    Args:
        DatasetClass (Type[Dataset]): Dataset class to be wrapped.

    Returns:
        Type[Dataset]: dataset with index.
    """

    class DatasetWithIndex(DatasetClass):
        def __getitem__(self, index):
            data = super().__getitem__(index)
            return (index, *data)

    return DatasetWithIndex


class CustomDatasetWithoutLabels(Dataset):
    def __init__(self, root, transform=None):
        self.root = Path(root)
        self.transform = transform
        self.images = os.listdir(root)

    def __getitem__(self, index):
        path = self.root / self.images[index]
        x = Image.open(path).convert("RGB")
        if self.transform is not None:
            x = self.transform(x)
        return x, -1

    def __len__(self):
        return len(self.images)


class NCropAugmentation:
    def __init__(self, transform: Callable, num_crops: int):
        """Creates a pipeline that apply a transformation pipeline multiple times.

        Args:
            transform (Callable): transformation pipeline.
            num_crops (int): number of crops to create from the transformation pipeline.
        """

        self.transform = transform
        self.num_crops = num_crops

    def __call__(self, x: Image) -> List[torch.Tensor]:
        """Applies transforms n times to generate n crops.

        Args:
            x (Image): an image in the PIL.Image format.

        Returns:
            List[torch.Tensor]: an image in the tensor format.
        """

        return [self.transform(x) for _ in range(self.num_crops)]

    def __repr__(self) -> str:
        return f"{self.num_crops} x [{self.transform}]"


class FullTransformPipeline:
    def __init__(self, transforms: Callable) -> None:
        self.transforms = transforms

    def __call__(self, x: Image) -> List[torch.Tensor]:
        """Applies transforms n times to generate n crops.

        Args:
            x (Image): an image in the PIL.Image format.

        Returns:
            List[torch.Tensor]: an image in the tensor format.
        """

        out = []
        for transform in self.transforms:
            out.extend(transform(x))
        return out

    def __repr__(self) -> str:
        return "\n".join([str(transform) for transform in self.transforms])


def imagenet_pipelines(
    brightness: float,
    contrast: float,
    saturation: float,
    hue: float,
    color_jitter_prob: float = 0.8,
    gray_scale_prob: float = 0.2,
    horizontal_flip_prob: float = 0.5,
    gaussian_prob: float = 0.5,
    solarization_prob: float = 0.0,
    min_scale: float = 0.08,
    max_scale: float = 1.0,
    crop_size: int = 224,
    device: int = 0,
):
    """Class that applies Imagenet transformations.

    Args:
        brightness (float): sampled uniformly in [max(0, 1 - brightness), 1 + brightness].
        contrast (float): sampled uniformly in [max(0, 1 - contrast), 1 + contrast].
        saturation (float): sampled uniformly in [max(0, 1 - saturation), 1 + saturation].
        hue (float): sampled uniformly in [-hue, hue].
        color_jitter_prob (float, optional): probability of applying color jitter.
            Defaults to 0.8.
        gray_scale_prob (float, optional): probability of converting to gray scale.
            Defaults to 0.2.
        horizontal_flip_prob (float, optional): probability of flipping horizontally.
            Defaults to 0.5.
        gaussian_prob (float, optional): probability of applying gaussian blur.
            Defaults to 0.0.
        solarization_prob (float, optional): probability of applying solarization.
            Defaults to 0.0.
        min_scale (float, optional): minimum scale of the crops. Defaults to 0.08.
        max_scale (float, optional): maximum scale of the crops. Defaults to 1.0.
        crop_size (int, optional): size of the crop. Defaults to 224.
        device (int, optional): gpu device of the current process. Defaults to 0.
    """

    # Data decoding and augmentation
    image_pipeline = [
        RandomResizedCropRGBImageDecoder((crop_size, crop_size), scale=(min_scale, max_scale)),
        transforms.RandomApply(
            [transforms.ColorJitter(brightness, contrast, saturation, hue)],
            p=color_jitter_prob,
        ),
        transforms.RandomGrayscale(p=gray_scale_prob),
        transforms.RandomApply([GaussianBlur()], p=gaussian_prob),
        transforms.RandomApply([Solarization()], p=solarization_prob),
        RandomHorizontalFlip(flip_prob=horizontal_flip_prob),
        ToTensor(),
        ToDevice(device, non_blocking=True),
        ToTorchImage(),
        NormalizeImage(mean=(0.485, 0.456, 0.406), std=(0.228, 0.224, 0.225), type=np.float16),
    ]
    label_pipeline = [IntDecoder(), ToTensor(), Squeeze(), ToDevice(device, non_blocking=True)]

    # Pipeline for each data field
    return {"image": image_pipeline, "label": label_pipeline}


def prepare_transform(dataset: str, **kwargs) -> Any:
    """Prepares transforms for a specific dataset. Optionally uses multi crop.

    Args:
        dataset (str): name of the dataset.

    Returns:
        Any: a transformation for a specific dataset.
    """

    if dataset in ["imagenet", "imagenet100"]:
        return imagenet_pipelines(**kwargs)
    else:
        raise ValueError(f"{dataset} is not currently supported.")


def prepare_n_crop_transform(
    transforms: List[Callable], num_crops_per_aug: List[int]
) -> NCropAugmentation:
    """Turns a single crop transformation to an N crops transformation.

    Args:
        transforms (List[Callable]): list of transformations.
        num_crops_per_aug (List[int]): number of crops per pipeline.

    Returns:
        NCropAugmentation: an N crop transformation.
    """

    assert len(transforms) == len(num_crops_per_aug)

    T = []
    for transform, num_crops in zip(transforms, num_crops_per_aug):
        T.append(NCropAugmentation(transform, num_crops))
    return FullTransformPipeline(T)


def prepare_dataloader(
    train_ffcv_dataset: str,
    pipelines: dict,
    batch_size: int = 64,
    num_workers: int = 4,
    distributed: bool = False,
    fit_mem: bool = False,
) -> Loader:
    """Prepares the training dataloader for pretraining.
    Args:
        train_dataset (Dataset): the name of the dataset.
        batch_size (int, optional): batch size. Defaults to 64.
        num_workers (int, optional): number of workers. Defaults to 4.
    Returns:
        Loader: the ffcv training dataloader with the desired dataset.
    """
    order = OrderOption.RANDOM if distributed else OrderOption.QUASI_RANDOM
    train_loader = Loader(
        train_ffcv_dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        order=order,
        os_cache=fit_mem,
        drop_last=True,
        pipelines=pipelines,
        distributed=distributed,
    )

    return train_loader
