import cv2
import numpy as np
from collections import Counter
from typing import Callable


def detect_continuous_axis(image, tol=0, region_thres=0, binary_thres=200, axis=1):
    if len(image.shape) == 3:
        # binary
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, image = cv2.threshold(image, binary_thres, 255, cv2.THRESH_BINARY_INV)

    projection = np.any(image, axis=axis)

    projection = np.insert(projection, 0, 0)
    projection = np.append(projection, 0)
    diff = np.diff(projection)
    start = np.argwhere(diff == 1).flatten()
    end = np.argwhere(diff == -1).flatten() - 1
    lines = np.stack((start, end), axis=1)

    idx = np.where((np.abs(lines[1:, 0] - lines[:-1, 1])) < tol)[0]

    for i in idx[::-1]:
        lines[i, 1] = lines[i + 1, 1]

    flag = np.ones(len(lines), dtype=bool)
    flag[idx + 1] = False
    lines = lines[flag]

    # length larger than region_thres
    lines = lines[(lines[:, 1] - lines[:, 0]) >= region_thres]

    return lines


def detect_continuous_areas(image, x_tol=20, y_tol=20, region_thres=0, binary_thres=200):
    if len(image.shape) == 3:
        # binary
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, image = cv2.threshold(image, binary_thres, 255, cv2.THRESH_BINARY_INV)

    y_lines = detect_continuous_axis(image, y_tol, region_thres, binary_thres, axis=1)

    bboxes = []

    for y_line in y_lines:
        x_lines = detect_continuous_axis(image[y_line[0]: y_line[1]], x_tol, region_thres, binary_thres, axis=0)
        bbox = np.zeros((len(x_lines), 4), dtype=int)
        bbox[:, 0::2] = x_lines
        bbox[:, 1::2] = y_line
        bboxes.append(bbox)

    bboxes = np.concatenate(bboxes, axis=0)

    return bboxes


class PixBox:
    @staticmethod
    def close(image, k_size=8):
        k = np.ones((k_size, k_size), np.uint8)
        return cv2.morphologyEx(image, cv2.MORPH_CLOSE, k)

    @staticmethod
    def open(image, k_size=8):
        k = np.ones((k_size, k_size), np.uint8)
        return cv2.morphologyEx(image, cv2.MORPH_OPEN, k)

    @staticmethod
    def pixel_to_box(pix_image, min_area=400, ignore_class=None, convert_func=None):
        """

        Args:
            pix_image: 2-d array
            min_area:
            ignore_class:
            convert_func: function to convert the mask

        Returns:

        """
        unique_classes = np.unique(pix_image)
        bboxes = []
        classes = []

        for c in unique_classes:
            if c in ignore_class:
                continue

            mask = (pix_image == c).astype(np.uint8)
            if convert_func:
                mask = convert_func(mask)

            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8, ltype=cv2.CV_16U)
            stats = stats[stats[:, 4] > min_area]

            stats[:, 2:4] = stats[:, :2] + stats[:, 2:4]
            box = stats[1:, :4]
            bboxes.append(box)
            classes.append([c] * len(box))

        bboxes = np.concatenate(bboxes, axis=0)
        classes = np.concatenate(classes, axis=0)

        return bboxes, classes

    @staticmethod
    def batch_pixel_to_box(pix_images, thres=0.5, min_area=400, ignore_class=None, convert_func=None):
        """

        Args:
            pix_images: 3-d array, (c, h, w), c gives the classes
            thres:
            min_area:
            ignore_class:
            convert_func: function to convert the mask

        Returns:

        """
        num_class = pix_images.shape[2]
        bboxes = []
        classes = []

        for c in range(num_class):
            if c in ignore_class:
                continue

            mask = (pix_images[c] > thres).astype(np.uint8)
            if convert_func:
                mask = convert_func(mask)

            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8, ltype=cv2.CV_16U)
            stats = stats[stats[:, 4] > min_area]

            stats[:, 2:4] = stats[:, :2] + stats[:, 2:4]
            box = stats[1:, :4]
            bboxes.append(box)
            classes.append([c] * len(box))

        return bboxes, classes

    @staticmethod
    def box_to_pixel(images, bboxes, classes, add_edge=False):
        h, w = images.shape[:2]
        pix_image = np.zeros((h, w), dtype=images.dtype)

        for box, cls in zip(bboxes, classes):
            x1, y1, x2, y2 = box
            pix_image[y1:y2, x1:x2] = cls

        if add_edge:
            for box, cls in zip(bboxes, classes):
                x1, y1, x2, y2 = box
                pix_image[y1:y2, x1 - 1 if x1 > 0 else x1] = 255
                pix_image[y1:y2, x2 + 1 if x2 < w else x2] = 255
                pix_image[y1 - 1 if y1 > 0 else y1, x1:x2] = 255
                pix_image[y2 + 1 if y2 < h else y2, x1:x2] = 255

        return pix_image


def fragment_image(image: np.ndarray,
                   max_size=None, grid=None,
                   overlap_size=None, overlap_ratio=None,
                   over_size=None, over_ratio=None
                   ):
    """fragment large image to small pieces

    Args:
        image:
        max_size:
        grid:
        overlap_size:
        overlap_ratio:
        over_size:
        over_ratio:

    Usage:

        >>> image = np.zeros((2000, 3000, 3))
        >>> images, coors = fragment_image(image, max_size=1000, overlap_size=100, over_ratio=0.5)
        >>> coors
        [(0, 0, 1000, 1000), (0, 900, 1000, 2000), (900, 0, 1900, 1000), (900, 900, 1900, 2000), (1800, 0, 3000, 1000), (1800, 900, 3000, 2000)]
        >>> [img.shape for img in images]
        [(1000, 1000, 3), (1100, 1000, 3), (1000, 1000, 3), (1100, 1000, 3), (1000, 1200, 3), (1100, 1200, 3)]
    """
    h, w = image.shape[:2]

    if max_size:
        size = (max_size, max_size) if isinstance(max_size, int) else max_size
    elif grid:
        # would be something wrong when overlap_size or overlap_ratio is not None
        size = (int(np.ceil(h / grid)), int(np.ceil(w / grid)))
    else:
        raise f'must be set max_size or grid, can not be None all of them'

    if overlap_size:
        overlap_size = (overlap_size, overlap_size) if isinstance(overlap_size, int) else overlap_size
    elif overlap_ratio:
        overlap_ratio = (overlap_ratio, overlap_ratio) if isinstance(overlap_ratio, float) else overlap_ratio
        overlap_size = (int(size[0] * overlap_ratio[0]), int(size[1] * overlap_ratio[1]))
    else:
        overlap_size = (0, 0)

    if over_size:
        over_size = (over_size, over_size) if isinstance(over_size, int) else over_size
    elif over_ratio:
        over_ratio = (over_ratio, over_ratio) if isinstance(over_ratio, float) else over_ratio
        over_size = (int(size[0] * over_ratio[0]), int(size[1] * over_ratio[1]))
    else:
        over_size = (0, 0)

    images = []
    coors = []
    if max_size:
        for i in range(0, w, size[0] - overlap_size[0]):
            for j in range(0, h, size[1] - overlap_size[1]):
                x1, y1, x2, y2 = i, j, min(i + size[0], w), min(j + size[1], h)

                if w - x1 < over_size[0] or h - y1 < over_size[1]:
                    continue

                remain = (w - x2, h - y2)
                if remain[0] < over_size[0]:
                    x2 = w
                if remain[1] < over_size[1]:
                    y2 = h

                coors.append((x1, y1, x2, y2))
                images.append(image[y1:y2, x1:x2])

    return images, coors


def non_max_suppression(boxes, conf, iou_method, threshold=0.6):
    """

    Args:
        boxes (np.ndarray): (n_samples， 4), 4 gives x1,y1,x2,y2
        conf (np.ndarray): (n_samples, )
        iou_method (Callable):
        threshold (float): IOU threshold

    Returns:
        keep (np.ndarray): 1-dim array, index of detections to keep
    """
    index = conf.argsort()[::-1]
    keep = []

    while index.size > 0:
        i = index[0]
        keep.append(i)

        ious = iou_method(boxes[i:i + 1], boxes[index[1:]])[0]
        inds = np.where(ious <= threshold)[0]
        index = index[inds + 1]

    return keep
