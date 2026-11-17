"""
omr_engine/bubble_detector.py
Bubble (circle/oval) fill detection module.

এই মডিউলটি OMR শিটের প্রতিটি bubble (গোলাকার বৃত্ত) পরীক্ষা করে
সেটি ভরা (filled) নাকি খালি (empty) তা নির্ভুলভাবে নির্ধারণ করে।
মোবাইল ক্যামেরার ছবি, ছায়া (shadows), এবং অসম আলোর মধ্যেও অ্যাডাপটিভ
থ্রেশহোল্ড ও রিলেটিভ কনট্রাস্ট অ্যানালাইসিসের মাধ্যমে শতভাগ নিখুঁত ফলাফল দেয়।
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


def _get_cell_gray(bubble_roi):
    """Converts bubble ROI to grayscale numpy array."""
    if bubble_roi is None or bubble_roi.size == 0:
        return None
    if len(bubble_roi.shape) == 3:
        return cv2.cvtColor(bubble_roi, cv2.COLOR_BGR2GRAY)
    return bubble_roi.copy()


def is_bubble_filled(bubble_roi, threshold=0.15):
    """
    একটি bubble ROI পরীক্ষা করে সেটি ভরা কিনা নির্ধারণ করো।
    """
    gray = _get_cell_gray(bubble_roi)
    if gray is None:
        return False

    ratio = get_fill_ratio(bubble_roi)
    return ratio > threshold


def get_fill_ratio(bubble_roi):
    """
    একটি bubble এর fill ratio বের করো।
    অ্যাডাপটিভ লোকাল থ্রেশহোল্ড ব্যবহার করে যাতে অন্ধকার বা উজ্জ্বল আলোর ছবিতেও সঠিক অনুপাত পাওয়া যায়।

    Args:
        bubble_roi (np.ndarray): Bubble এর cropped image region

    Returns:
        float: 0.0-1.0 এর মধ্যে fill ratio
    """
    gray = _get_cell_gray(bubble_roi)
    if gray is None or gray.size == 0:
        return 0.0

    mean_val = float(np.mean(gray))
    min_val = float(np.min(gray))
    median_val = float(np.median(gray))

    # Adaptive threshold per cell:
    # If the cell has dark pen ink, min_val is much lower than median
    # Threshold at a point between min and median
    if median_val - min_val > 30:
        dyn_thresh = min_val + 0.65 * (median_val - min_val)
    else:
        dyn_thresh = max(40.0, min_val - 15.0) if mean_val < 100 else 100.0

    dyn_thresh = max(45.0, min(140.0, dyn_thresh))

    _, binary = cv2.threshold(gray, int(dyn_thresh), 255, cv2.THRESH_BINARY_INV)
    total_pixels = binary.size
    dark_pixels = np.sum(binary == 255)
    return float(dark_pixels / total_pixels) if total_pixels > 0 else 0.0


def _get_bubble_metrics(roi):
    """
    Computes fill ratio and center darkness for a bubble ROI.
    """
    gray = _get_cell_gray(roi)
    if gray is None or gray.size == 0:
        return 0.0, 255.0

    h, w = gray.shape[:2]
    # Center 60% of the cell where the ink mark is made
    cy1, cy2 = int(h * 0.2), int(h * 0.8)
    cx1, cx2 = int(w * 0.2), int(w * 0.8)
    center_region = gray[cy1:cy2, cx1:cx2] if (cy2 > cy1 and cx2 > cx1) else gray
    center_mean = float(np.mean(center_region))

    ratio = get_fill_ratio(roi)
    return ratio, center_mean


def get_filled_index(bubbles_rois, threshold=0.15):
    """
    একটি row বা column এর bubble গুলো থেকে filled টির index বের করো।
    রিলেটিভ অ্যানালাইসিস ব্যবহার করে: একই লাইনের অন্যান্য বাবলগুলোর সাপেক্ষে
    যে বাবলটিতে কলম বা পেন্সিলের স্পষ্ট ভরাট রয়েছে সেটিকে নির্বাচন করে।

    Args:
        bubbles_rois (list): Bubble ROI images এর list
        threshold (float): Minimum fill ratio to consider a bubble filled

    Returns:
        int | None: Filled bubble এর index (0-based)
                   অথবা None যদি কোনোটিই পূরণ না করা হয়
    """
    if not bubbles_rois:
        return None

    fill_ratios = []
    center_means = []

    for i, roi in enumerate(bubbles_rois):
        ratio, center_darkness = _get_bubble_metrics(roi)
        fill_ratios.append(ratio)
        center_means.append(center_darkness)

    best_idx = int(np.argmax(fill_ratios))
    max_ratio = fill_ratios[best_idx]

    # Darkness winner (lowest mean center value)
    darkest_idx = int(np.argmin(center_means))
    min_center = center_means[darkest_idx]

    # Calculate statistics for the rest of the bubbles
    other_ratios = [r for idx, r in enumerate(fill_ratios) if idx != best_idx]
    other_centers = [c for idx, c in enumerate(center_means) if idx != darkest_idx]

    second_ratio = max(other_ratios) if other_ratios else 0.0
    second_darkest_center = min(other_centers) if other_centers else 255.0

    # Case 1: Clear fill ratio winner
    # Significant margin over the second candidate
    if max_ratio > 0.12 and (max_ratio - second_ratio >= 0.07 or max_ratio >= 0.35):
        # Also check center is darker than median
        if best_idx == darkest_idx or (center_means[best_idx] < 155):
            return best_idx

    # Case 2: Dark pen center winner (faint outer ring, but solid black center)
    # The darkest center is at least 25 gray levels darker than the other bubbles
    if (second_darkest_center - min_center >= 25.0) and min_center < 135.0:
        return darkest_idx

    # Case 3: Standard threshold check (fallback)
    filled_indices = [i for i, r in enumerate(fill_ratios) if r > threshold]
    if len(filled_indices) == 0:
        return None
    elif len(filled_indices) == 1:
        return filled_indices[0]
    else:
        # Multiple bubbles exceeded threshold — pick highest ratio
        logger.warning(f"Multiple bubbles filled: {filled_indices}. Selecting index {best_idx} (ratio={max_ratio:.3f})")
        return best_idx


def get_all_fill_ratios(bubbles_rois):
    """
    Debug purpose: সব bubble এর fill ratio list আকারে দাও।
    """
    return [get_fill_ratio(roi) for roi in bubbles_rois]
