"""
Copyright: Intel Corp. 2018
Author: Wenyi Tang
Email: wenyi.tang@intel.com
Created Date: May 8th 2018
Updated Date: May 24th 2018

Load files with specified filter in given directories,
and provide inheritable API for specific loaders.
"""
import numpy as np

from .Dataset import Dataset
from .VirtualFile import RawFile, ImageFile, _ALLOWED_RAW_FORMAT
from ..Util import ImageProcess, Utility


class Loader(object):

    def __init__(self, dataset, method, loop=False):
        """Initiate loader for given path `path`

        Args:
            dataset: dataset object, see Dataset.py
            method: 'train', 'val', or 'test'
            loop: if True, read data infinitely
        """
        if not isinstance(dataset, Dataset):
            raise TypeError('dataset must be Dataset object')
        dataset_file = dataset.__getattr__(method.lower())
        np.random.shuffle(dataset_file)
        self.length = len(dataset_file)
        self.mode = dataset.mode
        if self.mode.lower() == 'pil-image':
            self.dataset = [ImageFile(fp, loop) for fp in dataset_file]
        elif self.mode.upper() in _ALLOWED_RAW_FORMAT:
            self.dataset = [RawFile(
                fp, dataset.mode, (dataset.width, dataset.height), loop) for fp in dataset_file]
        self.patch_size = dataset.patch_size
        self.scale = dataset.scale
        self.strides = dataset.strides
        self.depth = dataset.depth
        self.batch_iterator = None
        self.loop = loop
        self.random = dataset.random and not (method == 'test')
        self.max_patches = dataset.max_patches
        self.built = False

    def __next__(self):
        if not self.built:
            raise RuntimeError(
                'This loader has not been built! Call **build_loader** first.')
        next(self.batch_iterator)

    def __iter__(self):
        return self.batch_iterator

    def __len__(self):
        if self.random:
            return self.max_patches
        else:
            n_patches = 0
            for vf in self.dataset:
                w, h = vf.shape
                sr = self.strides or [w, h]
                sz = self.patch_size or [w, h]
                n_patches += ((w - sz[0]) // sr[0] + 1) * ((h - sz[1]) // sr[1] + 1)
            return n_patches

    def _build_iter(self):
        while True:
            for vf in self.dataset:
                for _ in range(vf.frames // self.depth):
                    frames_hr = [ImageProcess.shrink_to_multiple_scale(img, self.scale) for img in
                                 vf.read_frame(self.depth)]
                    frames_lr = [ImageProcess.bicubic_rescale(
                        img, np.ones(2) / self.scale) for img in frames_hr]
                    width, height = frames_hr[0].size
                    strides = self.strides or [width, height]
                    patch_size = self.patch_size or [width, height]
                    for w in range(0, width, strides[0]):
                        for h in range(0, height, strides[1]):
                            if w + patch_size[0] > width or h + patch_size[1] > height:
                                continue
                            box = np.array([w, h, w + patch_size[0], h + patch_size[1]])
                            crop_hr = [img.crop(box) for img in frames_hr]
                            crop_lr = [img.crop(box // [*self.scale, *self.scale]) for img in frames_lr]
                            yield crop_hr, crop_lr
                vf.read_frame(vf.frames)
            if not self.loop:
                break

    def _build_random_iter(self):
        patch_counter = 0
        patch_per_file = self.max_patches // self.length
        patch_per_file += 1 if patch_per_file != self.max_patches / self.length else 0
        for vf in self.dataset:
            vf.reopen() if not vf.frames else None
            for _ in range(vf.frames // self.depth):
                frames_hr = [ImageProcess.shrink_to_multiple_scale(img, self.scale) for img in
                             vf.read_frame(self.depth)]
                frames_lr = [ImageProcess.bicubic_rescale(
                    img, np.ones(2) / self.scale) for img in frames_hr]
                width, height = frames_hr[0].size
                patch_size = self.patch_size or [width, height]
                for _ in range(patch_per_file):
                    if patch_counter >= self.max_patches:
                        raise StopIteration()
                    x = np.random.randint(0, width - patch_size[0] + 1)
                    y = np.random.randint(0, height - patch_size[1] + 1)
                    box = np.array([x, y, x + patch_size[0], y + patch_size[1]])
                    crop_hr = [img.crop(box) for img in frames_hr]
                    crop_lr = [img.crop(box // [*self.scale, *self.scale]) for img in frames_lr]
                    patch_counter += 1
                    yield crop_hr, crop_lr

    def build_loader(self, crop=True, **kwargs):
        """Build image(s) pair loader, make self iterable

         Args:
             crop: if True, crop the images into patches
             kwargs: you can override attribute in the dataset
        """
        _crop_args = [
            'scale',
            'patch_size',
            'strides',
            'depth'
        ]
        for _arg in _crop_args:
            if _arg in kwargs and kwargs[_arg]:
                self.__setattr__(_arg, kwargs[_arg])

        self.scale = Utility.to_list(self.scale, 2)
        self.patch_size = Utility.to_list(self.patch_size, 2)
        self.strides = Utility.to_list(self.strides, 2)
        self.patch_size = Utility.shrink_mod_scale(self.patch_size, self.scale) if crop else None
        self.strides = Utility.shrink_mod_scale(self.strides, self.scale) if crop else None
        if self.random:
            self.batch_iterator = self._build_random_iter()
        else:
            self.batch_iterator = self._build_iter()
        self.built = True


class BatchLoader:

    def __init__(self,
                 batch_size,
                 dataset,
                 method,
                 loop=False,
                 convert_to_gray=True,
                 **kwargs):
        """Build an iterable to load datasets in batch size

        Args:
            batch_size: an integer, the size of a batch
            dataset: an instance of Dataset, see DataLoader.Dataset
            method: 'train', 'val', or 'test', each for different files in datasets
            loop: if True, iterates infinitely
            kwargs: you can override attribute in the dataset
        """
        self.loader = Loader(dataset, method, loop)
        self.loader.build_loader(**kwargs)
        self.batch = batch_size
        self.to_gray = convert_to_gray

    def __iter__(self):
        return self

    def __next__(self):
        hr, lr = self._load_batch()
        if isinstance(hr, np.ndarray) and isinstance(lr, np.ndarray):
            try:
                return np.squeeze(hr, 1), np.squeeze(lr, 1)
            except ValueError:
                return hr, lr
        raise StopIteration('End BatchLoader!')

    def __len__(self):
        """Total iteration steps"""
        steps = np.ceil(len(self.loader) / self.batch)
        return int(steps)

    def _load_batch(self):
        batch_hr, batch_lr = [], []
        for hr, lr in self.loader:
            if self.to_gray:
                hr = [img.convert('L') for img in hr]
                lr = [img.convert('L') for img in lr]
            else:
                hr = [img.convert('YCbCr') for img in hr]
                lr = [img.convert('YCbCr') for img in lr]
            batch_hr.append(np.stack([ImageProcess.img_to_array(img) for img in hr]))
            batch_lr.append(np.stack([ImageProcess.img_to_array(img) for img in lr]))
            if len(batch_hr) == self.batch:
                return np.stack(batch_hr), np.stack(batch_lr)
        if batch_hr and batch_lr:
            return np.stack(batch_hr), np.stack(batch_lr)
        return [], []
