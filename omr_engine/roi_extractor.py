"""
omr_engine/roi_extractor.py
Region of Interest (ROI) extraction utilities.

এই মডিউলটি template JSON অনুযায়ী image থেকে
নির্দিষ্ট অঞ্চল (ROI) বের করার utility functions সরবরাহ করে।
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


def extract_roi(image, region):
    """
    Template region definition অনুযায়ী image থেকে ROI crop করো।

    Args:
        image (np.ndarray): Source image
        region (dict): Region definition with keys:
                       'x', 'y', 'w', 'h' (all in pixels)

    Returns:
        np.ndarray: Cropped ROI image
                   অথবা empty array যদি region invalid হয়
    """
    x = int(region.get('x', 0))
    y = int(region.get('y', 0))
    w = int(region.get('w', 0))
    h = int(region.get('h', 0))

    # Image boundary check
    img_h, img_w = image.shape[:2]
    x = max(0, min(x, img_w - 1))
    y = max(0, min(y, img_h - 1))
    w = min(w, img_w - x)
    h = min(h, img_h - y)

    if w <= 0 or h <= 0:
        logger.error(f"Invalid ROI region: x={x}, y={y}, w={w}, h={h}")
        return np.array([])

    roi = image[y:y + h, x:x + w]
    logger.debug(f"Extracted ROI: ({x},{y}) size={w}x{h}")
    return roi


def split_grid(roi, rows, cols):
    """
    একটি ROI কে rows×cols grid এ সমান ভাগ করো।

    প্রতিটি cell এর size সমান হয় (integer division)।
    Grid বাইরের অতিরিক্ত pixels (যদি থাকে) বাদ দেওয়া হয়।

    Args:
        roi (np.ndarray): Source ROI image
        rows (int): সারির সংখ্যা
        cols (int): কলামের সংখ্যা

    Returns:
        list: 2D list of cell images [row_idx][col_idx]
              যেমন cells[2][1] = 3য় সারির 2য় কলামের cell
    """
    if roi is None or roi.size == 0:
        logger.error("Empty ROI passed to split_grid")
        return [[np.array([])] * cols for _ in range(rows)]

    h, w = roi.shape[:2]
    cell_h = h // rows
    cell_w = w // cols

    if cell_h == 0 or cell_w == 0:
        logger.error(f"Cell size too small: {cell_w}x{cell_h} for {cols}x{rows} grid in {w}x{h} ROI")
        return [[np.array([])] * cols for _ in range(rows)]

    cells = []
    for r in range(rows):
        row_cells = []
        for c in range(cols):
            # Cell boundaries হিসাব করো
            y1 = r * cell_h
            y2 = (r + 1) * cell_h
            x1 = c * cell_w
            x2 = (c + 1) * cell_w
            cell = roi[y1:y2, x1:x2]
            row_cells.append(cell)
        cells.append(row_cells)

    return cells


def split_grid_with_padding(roi, rows, cols, padding=2):
    """
    Grid এ ভাগ করো এবং প্রতিটি cell এর edge থেকে padding বাদ দাও।

    Cell edge এ printing artifacts থাকলে এটি bubble detection accuracy বাড়ায়।

    Args:
        roi (np.ndarray): Source ROI image
        rows (int): সারির সংখ্যা
        cols (int): কলামের সংখ্যা
        padding (int): প্রতিটি cell এর প্রতিটি edge থেকে বাদ দেওয়ার pixels

    Returns:
        list: 2D list of padded cell images
    """
    cells = split_grid(roi, rows, cols)
    padded_cells = []

    for row in cells:
        padded_row = []
        for cell in row:
            if cell is not None and cell.size > 0:
                h, w = cell.shape[:2]
                # Padding apply করো (cell বড় হতে হবে)
                if h > 2 * padding and w > 2 * padding:
                    padded_cell = cell[padding:h - padding, padding:w - padding]
                else:
                    padded_cell = cell
                padded_row.append(padded_cell)
            else:
                padded_row.append(cell)
        padded_cells.append(padded_row)

    return padded_cells


def save_debug_roi(image, region, output_path, label="roi"):
    """
    Debug purpose: ROI বের করে save করো।

    Args:
        image (np.ndarray): Source image
        region (dict): Region definition
        output_path (str): Output file path
        label (str): Debug label
    """
    roi = extract_roi(image, region)
    if roi.size > 0:
        # Label overlay করো
        debug_img = roi.copy()
        cv2.putText(debug_img, label, (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imwrite(str(output_path), debug_img)
        logger.info(f"Debug ROI saved: {output_path}")
