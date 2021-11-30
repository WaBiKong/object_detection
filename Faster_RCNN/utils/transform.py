# -*- coding: utf-8 -*-#

# -------------------------------------------------------------------------------
# Name:         transform.py
# Description:  GeneralizedRCNNTransform in network，包括标准化才处理，resize，打包成batch和postproces(把bboxes还原到原尺寸图象)
# Author:       WaBiKong
# Date:         2021/11/17
# -------------------------------------------------------------------------------

import math
from typing import List, Tuple

import torch
import torch.nn.functional as F
import torchvision
from torch import nn

from Faster_RCNN.utils.image_list import ImageList


class GeneralizedRCNNTransform(nn.Module):
    def __init__(self, min_size, max_size, image_mean, image_std):
        super(GeneralizedRCNNTransform, self).__init__()
        if not isinstance(min_size, (list, tuple)):
            min_size = (min_size,)  # 转成tuple类型
        self.min_size = min_size  # 指定图像的最小边长范围
        self.max_size = max_size  # 指定图像的最大边长范围
        self.image_mean = image_mean  # 指定图像在标准化处理中的均值
        self.image_std = image_std  # 指定图像在标准化处理中的方差

    def normalize(self, image):
        """标准化处理"""
        dtype, device = image.dtype, image.device
        mean = torch.as_tensor(self.image_mean, dtype=dtype, device=device)
        std = torch.as_tensor(self.image_std, dtype=dtype, device=device)
        # [:, None, None]: shape [3] -> [3, 1, 1]
        # 减去均值后再除以方差
        return (image - mean[:, None, None]) / std[:, None, None]

    def resize(self, image, target):
        # type: (Tensor, Optional[DIct[str, Tensor]]) -> Tuple[Tensor, Optional[Dict[str, Tensor]]]
        """
        将图片缩放到指定大小范围内，并相应缩放 bboxes信息
        Args:
            image: 输入的图片
            target: 输入图片的相关信息（包括 bboxes）

        Returns:
            image: 缩放后的图片
            target: 缩放 bboxes后的图片的相关信息
        """

        # image shape is [channel, height, width]
        h, w = image.shape[-2:]
        im_shape = torch.tensor(image.shape[-2:])
        min_size = float(torch.min(im_shape))  # 获取长宽中的最小值
        max_size = float(torch.max(im_shape))  # 获取长宽中的最大值
        if self.training:  # 训练模式
            size = float(self.torch_choice(self.min_size))  # 指定输入图片的最小边长，注意是self.min_size，不是min_size
        else:  # 验证模式
            # FIXME assume for now that testing uses the largest scale
            size = float(self.min_size[-1])  # 指定输入图片的最小边长，注意是self.min_size，不是min_size
        scale_factor = size / min_size  # 根据指定最小边长和图片最小边长计算缩放比例

        # 如果使用该缩放比例计算的图片的边长大于指定的最大边长
        if max_size * scale_factor > self.max_size:
            scale_factor = self.max_size / max_size  # 将缩放比例设为指定最大边长和图片最大边长之比

        # interpolate利用插值的方法缩放图片
        # image[None]操作是在最前面添加batch维度[c, h, w] -> [1, c, h, w]
        # bilinear双线性插值只支持4D Tensor
        image = F.interpolate(image[None], scale_factor=scale_factor,
                              mode='bilinear', align_corners=False)
        image = image[0]  # 通过索引转换回3D Tensor

        if target is None:
            return image, target

        bbox = target['boxes']
        # 根据图像的缩放比例来缩放bbox
        # (h, w): 原始尺寸
        # image.shape[-2:]: 缩放后的高度宽度
        bbox = resize_boxes(bbox, [h, w], image.shape[-2:])
        target['boxes'] = bbox

        return image, target

    def batch_images(self, images, size_divisible=32):
        # type: (List[Tensor], int) -> Tensor
        """
        将一批图像打包成一个batch返回（注意batch中每个tensor的shape是相同的）
        Args:
            images: 输入的一批图片
            size_divisible: 将图像高和宽调整到该数的整数倍
        Returns:
            batched_imgs: 打包成一个batch后的tensor数据
        """

        if torchvision._is_tracing():
            # batch_images() does not export well to ONNX
            # call _onnx_batch_images() instead
            return self._onnx_batch_images(images, size_divisible)

        # 分别计算一个batch中所有图片中的最大channel, height, width
        max_size = self.max_by_axis([list(img.shape) for img in images])

        stride = float(size_divisible)
        # 将height向上调整到stride的整数倍
        max_size[1] = int(math.ceil(float(max_size[1]) / stride) * stride)
        # 将width向上调整到stride的整数倍
        max_size[2] = int(math.ceil(float(max_size[2]) / stride) * stride)

        # [batch, channel, height, width]
        batch_shape = [len(images)] + max_size

        # 创建shape为batch_shape、值全为0的tensor
        # image[0]是Tensor，是哪个都无所谓，只是为了利用Tensor下的new_full方法
        batched_imgs = images[0].new_full(batch_shape, 0)
        for img, pad_img in zip(images, batched_imgs):
            # 将输入images中的每张图片复制到新的batched_imgs的每张图片中，对齐左上角，保证bboxes的坐标不变
            # 这样保证输入到网络中一个batch的每张图片的shape相同
            # copy_: Copies the elements from src into self tensor and returns self
            # [: img.shape[1]]: 切片获得0到img第1维大小的像素点, 0开始是为了左上角对齐
            pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)

        return batched_imgs

    def postprocess(self, result, image_shapes, original_image_sizes):
        # type: (List[Dict[str, Tensor]], List[Tuple[int, int]], List[Tuple[int, int]]) -> List[Dict[str, Tensor]]
        """
        对网络的预测结果进行后处理（主要将bboxes还原到原图像尺度上）
        Args:
            result: list(dict), 网络的预测结果, len(result) == batch_size
            image_shapes: list(torch.Size), 图像预处理缩放后的尺寸, len(image_shapes) == batch_size
            original_image_sizes: list(torch.Size), 图像的原始尺寸, len(original_image_sizes) == batch_size
        Returns:
        """
        if self.training:  # 训练模式只需要获得损失进行反向传播
            return result

        # 预测模式下
        # 遍历每张图片的预测信息，将boxes信息还原回原尺度
        for i, (pred, im_s, o_im_s) in enumerate(zip(result, image_shapes, original_image_sizes)):
            boxes = pred["boxes"]
            boxes = resize_boxes(boxes, im_s, o_im_s)  # 将bboxes缩放回原图像尺度上
            result[i]["boxes"] = boxes
        return result

    def forward(self, images, targets=None):
        images = [img for img in images]
        for i in range(len(images)):
            image = images[i]
            target_index = targets[i] if targets is not None else None

            if image.dim() != 3:  # 验证输入的图片维度数是否为3
                raise ValueError("images is expected to be a list of 3d tensors"
                                 f" of shape [C, H, W], got{image.shape}")
            image = self.normalize(image)  # 对图像进行标准化处理
            image, target_index = self.resize(image, target_index)  # 将图像和对应的bboxes缩放到指定的范围内
            images[i] = image
            if targets is not None and target_index is not None:
                targets[i] = target_index

        # 记录resize后的图像尺寸
        image_sizes = [img.shape[-2:] for img in images]

        # 将images打包成一个batch
        images = self.batch_images(images)

        image_sizes_list = torch.jit.annotate(List[Tuple[int, int]], [])
        for image_size in image_sizes:
            assert len(image_size) == 2
            image_sizes_list.append((image_size[0], image_size[1]))

        image_list = ImageList(images, image_sizes_list)  # 将图片和resize后的大小关联起来
        return image_list, targets

    def torch_choice(self, k):
        # type: (List[int]) -> int
        """
        Implements `random.choice` via torch ops so it can be compiled with
        TorchScript. Remove if https://github.com/pytorch/pytorch/issues/25803
        is fixed.
        """
        index = int(torch.empty(1).uniform_(0., float(len(k))).item())
        return k[index]

    @torch.jit.unused
    def _onnx_batch_images(self, images, size_divisible=32):
        # type: (List[Tensor], int) -> Tensor
        max_size = []
        for i in range(images[0].dim()):
            max_size_i = torch.max(torch.stack([img.shape[i] for img in images]).to(torch.float32)).to(torch.int64)
            max_size.append(max_size_i)
        stride = size_divisible
        max_size[1] = (torch.ceil((max_size[1].to(torch.float32)) / stride) * stride).to(torch.int64)
        max_size[2] = (torch.ceil((max_size[2].to(torch.float32)) / stride) * stride).to(torch.int64)
        max_size = tuple(max_size)

        # work around for
        # pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)
        # which is not yet supported in onnx
        padded_imgs = []
        for img in images:
            padding = [(s1 - s2) for s1, s2 in zip(max_size, tuple(img.shape))]
            padded_img = torch.nn.functional.pad(img, [0, padding[2], 0, padding[1], 0, padding[0]])
            padded_imgs.append(padded_img)

        return torch.stack(padded_imgs)

    def max_by_axis(self, the_list):
        # type: (List[List[int]]) -> List[int]
        maxes = the_list[0]
        for sublist in the_list[1:]:
            for index, item in enumerate(sublist):
                maxes[index] = max(maxes[index], item)
        return maxes

    def __repr__(self):
        """自定义输出实例化对象的信息，可通过print打印实例信息"""
        format_string = self.__class__.__name__ + '('
        _indent = '\n    '
        format_string += "{0}Normalize(mean={1}, std={2})".format(_indent, self.image_mean, self.image_std)
        format_string += "{0}Resize(min_size={1}, max_size={2}, mode='bilinear')".format(_indent, self.min_size,
                                                                                         self.max_size)
        format_string += '\n)'
        return format_string


def resize_boxes(boxes, original_size, new_size):
    # type: (Tensor, List[int], List[int]) -> Tensor
    """
    将boxes参数根据图像的缩放情况进行相应缩放
    Arguments:
        original_size: 图像缩放前的尺寸
        new_size: 图像缩放后的尺寸
    """

    # 缩放后 / 原始
    ratios = [
        torch.tensor(s, dtype=torch.float32, device=boxes.device) /
        torch.tensor(s_orig, dtype=torch.float32, device=boxes.device)
        for s, s_orig in zip(new_size, original_size)
    ]
    ratios_height, ratios_width = ratios
    # Removes a tensor dimension, boxes [minibatch, 4]
    # Returns a tuple of all slices along a given dimension, already without it.
    xmin, ymin, xmax, ymax = boxes.unbind(1)  # 在第1维度上进行展开，即4这个维度
    xmin = xmin * ratios_width
    xmax = xmax * ratios_width
    ymin = ymin * ratios_height
    ymax = ymax * ratios_height
    return torch.stack((xmin, ymin, xmax, ymax), dim=1)  # 在第1维度上进行合并
