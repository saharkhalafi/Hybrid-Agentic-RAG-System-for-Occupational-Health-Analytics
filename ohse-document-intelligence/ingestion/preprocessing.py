"""Image preprocessing for scanned pages."""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

from config.logging import get_logger

logger = get_logger(__name__)


def preprocess_page_image(image_bytes: bytes) -> bytes:
    """Apply deskew, denoise, and contrast enhancement."""
    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    array = np.array(image)

    denoised = cv2.fastNlMeansDenoising(array, None, 10, 7, 21)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)

    output = io.BytesIO()
    Image.fromarray(enhanced).save(output, format="PNG")
    return output.getvalue()
