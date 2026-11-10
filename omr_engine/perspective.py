"""
omr_engine/perspective.py
Perspective correction module for OMR sheet processing.

এই মডিউলটি OMR শিটের ৪টি কোণার কালো ■ চিহ্ন খুঁজে বের করে
perspective transform দিয়ে শিটটিকে সোজা করে।
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


def find_corner_markers(image, min_area=200, max_area=2000, aspect_tolerance=0.3):
    """
    OMR শিটের ৪টি কোণার কালো ■ চিহ্ন খুঁজে বের করো।

    Algorithm:
    1. Grayscale + threshold দিয়ে dark pixels খোঁজো
    2. Contour detection দিয়ে সম্ভাব্য বর্গক্ষেত্র খোঁজো
    3. TL, TR, BL, BR অনুযায়ী sort করো

    Args:
        image (np.ndarray): BGR image (OpenCV format)
        min_area (int): কোণার চিহ্নের সর্বনিম্ন area (pixels)
        max_area (int): কোণার চিহ্নের সর্বোচ্চ area (pixels)
        aspect_tolerance (float): Width/Height ratio tolerance (1.0 = perfect square)

    Returns:
        list: 4টি corner points [(x,y), ...] যেমন [TL, TR, BL, BR]
              অথবা None যদি ৪টি corner না পাওয়া যায়
    """
    # Grayscale রূপান্তর
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image.copy()

    # Threshold — dark pixels (কালো চিহ্ন) সনাক্ত করো
    # THRESH_BINARY_INV: কালো → সাদা, সাদা → কালো (processing সহজ হয়)
    _, thresh = cv2.threshold(gray, 60, 255, cv2.THRESH_BINARY_INV)

    # Morphological operations দিয়ে noise দূর করো
    kernel = np.ones((3, 3), np.uint8)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

    # Contour খোঁজো (external contours only)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    squares = []
    for c in contours:
        area = cv2.contourArea(c)

        # Area filter — খুব ছোট বা বড় contour বাদ দাও
        if not (min_area < area < max_area):
            continue

        # Polygon approximation — ৪-কোণা (বর্গাকার) shape খোঁজো
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.04 * peri, True)

        if len(approx) == 4:
            x, y, w, h = cv2.boundingRect(approx)
            aspect = w / float(h) if h > 0 else 0

            # Aspect ratio চেক — roughly square হতে হবে
            if abs(1.0 - aspect) < aspect_tolerance:
                # Center point হিসাব করো
                cx = x + w // 2
                cy = y + h // 2
                squares.append((cx, cy))
                logger.debug(f"Square found at ({cx}, {cy}), area={area:.0f}, aspect={aspect:.2f}")

    logger.info(f"Found {len(squares)} potential corner markers")

    if len(squares) < 4:
        logger.warning(f"Not enough corners found: {len(squares)}/4")
        return None

    # যদি ৪-এর বেশি পাওয়া যায়, সবচেয়ে extreme (corner-এ) গুলো নাও
    if len(squares) > 4:
        squares = _select_best_corners(squares, image.shape)

    # TL, TR, BL, BR অনুযায়ী sort করো
    return _sort_corners(squares)


def _select_best_corners(candidates, image_shape):
    """
    একাধিক candidate থেকে সবচেয়ে ভালো ৪টি corner বেছে নাও।
    Image-এর ৪টি কোণার সবচেয়ে কাছের points নাও।

    Args:
        candidates (list): সম্ভাব্য corner points
        image_shape (tuple): image এর shape (h, w, ...)

    Returns:
        list: সেরা ৪টি corner points
    """
    h, w = image_shape[:2]
    # ৪টি ideal corner position
    ideal_corners = [(0, 0), (w, 0), (0, h), (w, h)]

    selected = []
    used = set()

    for ideal in ideal_corners:
        min_dist = float('inf')
        best_idx = -1
        for i, pt in enumerate(candidates):
            if i in used:
                continue
            dist = ((pt[0] - ideal[0]) ** 2 + (pt[1] - ideal[1]) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                best_idx = i
        if best_idx >= 0:
            selected.append(candidates[best_idx])
            used.add(best_idx)

    return selected


def _sort_corners(corners):
    """
    ৪টি corner কে TL, TR, BL, BR ক্রমে সাজাও।

    Args:
        corners (list): 4টি (x, y) points

    Returns:
        list: [TL, TR, BL, BR] ক্রমে sorted corners
    """
    # Y অনুযায়ী sort করে top 2 এবং bottom 2 আলাদা করো
    corners_sorted_y = sorted(corners, key=lambda p: p[1])
    top = sorted(corners_sorted_y[:2], key=lambda p: p[0])    # X অনুযায়ী → TL, TR
    bottom = sorted(corners_sorted_y[-2:], key=lambda p: p[0])  # X অনুযায়ী → BL, BR

    tl, tr = top
    bl, br = bottom
    return [tl, tr, bl, br]


def warp_sheet(image, corners, output_size=(794, 1123)):
    """
    Perspective transform দিয়ে OMR শিট সোজা করো।

    Camera angle বা placement এর কারণে শিট বাঁকা থাকতে পারে।
    এই function টি ৪টি corner point ব্যবহার করে শিটকে
    সঠিক A4 অনুপাতে (portrait) রূপান্তর করে।

    Args:
        image (np.ndarray): Input BGR image
        corners (list): [TL, TR, BL, BR] corner points
        output_size (tuple): Output image size (width, height)
                            Default: 794×1123 (A4 at 96 DPI)

    Returns:
        np.ndarray: Perspective-corrected warped image
    """
    tl, tr, bl, br = corners

    # Source points — original image এর corner positions
    pts_src = np.float32([tl, tr, bl, br])

    # Destination points — output image এর corner positions
    w, h = output_size
    pts_dst = np.float32([
        [0, 0],       # TL
        [w, 0],       # TR
        [0, h],       # BL
        [w, h]        # BR
    ])

    # Perspective transformation matrix হিসাব করো
    M = cv2.getPerspectiveTransform(pts_src, pts_dst)

    # Transformation apply করো
    warped = cv2.warpPerspective(image, M, (w, h))

    logger.info(f"Perspective correction applied. Output size: {w}x{h}")
    return warped


def preprocess_image(image):
    """
    Image preprocessing — noise reduction এবং contrast enhancement।

    OMR reading accuracy বাড়ানোর জন্য image pre-process করো।

    Args:
        image (np.ndarray): Input BGR image

    Returns:
        np.ndarray: Preprocessed BGR image
    """
    # Gaussian blur দিয়ে noise কমাও
    blurred = cv2.GaussianBlur(image, (3, 3), 0)

    # Contrast Limited Adaptive Histogram Equalization (CLAHE)
    # এটি local contrast উন্নত করে
    gray = cv2.cvtColor(blurred, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # আবার BGR এ ফেরত নাও
    result = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
    return result
