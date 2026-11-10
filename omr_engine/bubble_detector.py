"""
omr_engine/bubble_detector.py
Bubble (circle/oval) fill detection module.

এই মডিউলটি OMR শিটের প্রতিটি bubble (গোলাকার বৃত্ত) পরীক্ষা করে
সেটি ভরা (filled) নাকি খালি (empty) তা নির্ধারণ করে।
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


def is_bubble_filled(bubble_roi, threshold=0.15):
    """
    একটি bubble ROI পরীক্ষা করে সেটি ভরা কিনা নির্ধারণ করো।

    একটি bubble "filled" ধরা হয় যখন তার মধ্যে dark pixels এর
    অনুপাত threshold-এর বেশি হয়।

    Args:
        bubble_roi (np.ndarray): Bubble এর cropped image region
        threshold (float): Dark pixel ratio এর threshold (0.0-1.0)
                          Default: 0.45 (45% dark হলে filled ধরবো)

    Returns:
        bool: True যদি bubble filled হয়, False যদি empty হয়
    """
    # Grayscale এ রূপান্তর (যদি BGR হয়)
    if len(bubble_roi.shape) == 3:
        gray = cv2.cvtColor(bubble_roi, cv2.COLOR_BGR2GRAY)
    else:
        gray = bubble_roi.copy()

    # Adaptive threshold ব্যবহার করো (better accuracy for varying lighting)
    # THRESH_BINARY_INV: dark areas → white (255)
    _, binary = cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY_INV)

    # Dark pixel এর অনুপাত হিসাব করো
    total_pixels = binary.size
    dark_pixels = np.sum(binary == 255)
    filled_ratio = dark_pixels / total_pixels if total_pixels > 0 else 0.0

    return filled_ratio > threshold


def get_fill_ratio(bubble_roi):
    """
    একটি bubble এর fill ratio বের করো।

    Args:
        bubble_roi (np.ndarray): Bubble এর cropped image region

    Returns:
        float: 0.0-1.0 এর মধ্যে fill ratio
    """
    if len(bubble_roi.shape) == 3:
        gray = cv2.cvtColor(bubble_roi, cv2.COLOR_BGR2GRAY)
    else:
        gray = bubble_roi.copy()

    _, binary = cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY_INV)
    total_pixels = binary.size
    dark_pixels = np.sum(binary == 255)
    return dark_pixels / total_pixels if total_pixels > 0 else 0.0


def get_filled_index(bubbles_rois, threshold=0.15):
    """
    একটি row বা column এর bubble গুলো থেকে filled টির index বের করো।

    একাধিক bubble filled হলে সবচেয়ে বেশি ভরা (highest fill ratio) টিকে
    নির্বাচন করা হয়।

    Args:
        bubbles_rois (list): Bubble ROI images এর list
                            (একটি প্রশ্নের A, B, C, D options)
        threshold (float): Minimum fill ratio to consider a bubble filled

    Returns:
        int | None: Filled bubble এর index (0-based)
                   অথবা None যদি কোনোটিই না হয়
    """
    fill_ratios = []
    filled_indices = []

    # প্রতিটি bubble এর fill ratio হিসাব করো
    for i, roi in enumerate(bubbles_rois):
        # ROI ফাঁকা হলে skip করো
        if roi is None or roi.size == 0:
            fill_ratios.append(0.0)
            continue

        ratio = get_fill_ratio(roi)
        fill_ratios.append(ratio)

        if ratio > threshold:
            filled_indices.append(i)
            logger.debug(f"Bubble {i} filled with ratio {ratio:.3f}")

    # কোনোটিই filled নয় → উত্তর দেওয়া হয়নি
    if len(filled_indices) == 0:
        logger.debug(f"No bubble filled. Ratios: {[f'{r:.3f}' for r in fill_ratios]}")
        return None

    # শুধু একটি filled → সেটিই উত্তর
    if len(filled_indices) == 1:
        return filled_indices[0]

    # একাধিক filled → সবচেয়ে বেশি ভরাটিকে নাও (ambiguous marking handle)
    best_idx = int(np.argmax(fill_ratios))
    logger.warning(f"Multiple bubbles filled: {filled_indices}. Selecting index {best_idx} (ratio={fill_ratios[best_idx]:.3f})")
    return best_idx


def get_all_fill_ratios(bubbles_rois):
    """
    Debug purpose: সব bubble এর fill ratio list আকারে দাও।

    Args:
        bubbles_rois (list): Bubble ROI images এর list

    Returns:
        list: প্রতিটি bubble এর fill ratio
    """
    return [get_fill_ratio(roi) for roi in bubbles_rois]
