"""
integration/laravel_client.py
HTTP Client to communicate with Laravel backend (/api/omr/*).

Laravel 7 Admission Challenge system:
- Class test roster & student matching (by numeric reg_no)
- Answer keys (Sets A, B, C, D)
- Result synchronization
"""

import os
import logging
import requests
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# Default to local artisan serve or virtual host
DEFAULT_BASE_URL = os.getenv("LARAVEL_API_URL", "http://127.0.0.1:8000/api/omr")


class LaravelOmrClient:
    """Client for Laravel OMR API endpoints."""

    def __init__(self, base_url: Optional[str] = None, timeout: int = 10):
        url = base_url or DEFAULT_BASE_URL
        # Ensure trailing slash stripped
        self.base_url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def get_class_test_info(self, class_test_id: int) -> Dict[str, Any]:
        """Fetch exam details and student roster for a class test."""
        url = f"{self.base_url}/classTest/{class_test_id}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_answer_key(self, class_test_id: int, exam_set: str) -> Optional[Dict[str, Any]]:
        """Fetch answer key for a class test and exam set (A/B/C/D)."""
        url = f"{self.base_url}/answerKey/{class_test_id}/{exam_set.upper()}"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def save_answer_key(
        self,
        class_test_id: int,
        exam_set: str,
        answers: Dict[str, str],
        correct_mark: float = 1.0,
        wrong_mark: float = -0.25,
    ) -> Dict[str, Any]:
        """Save or update an answer key in Laravel."""
        url = f"{self.base_url}/answerKey"
        payload = {
            "class_test_id": class_test_id,
            "set": exam_set.upper(),
            "answers": answers,
            "correct_mark": correct_mark,
            "wrong_mark": wrong_mark,
        }
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def find_student(self, class_test_id: int, numeric_id: str) -> Optional[Dict[str, Any]]:
        """
        Find a student enrolled in the test matching the numeric ID (digits of reg_no).
        Returns student dict with 'item_id', 'student_id', 'name', 'reg_no' or None.
        """
        url = f"{self.base_url}/students"
        params = {
            "class_test_id": class_test_id,
            "numeric_id": str(numeric_id).strip(),
        }
        resp = self.session.get(url, params=params, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def update_result(
        self,
        item_id: int,
        marks: float,
        correct_answer: Optional[int] = None,
        wrong_answer: Optional[int] = None,
        is_absend: int = 0,
    ) -> Dict[str, Any]:
        """Update single student test item result."""
        url = f"{self.base_url}/result"
        payload = {
            "item_id": item_id,
            "marks": marks,
            "correct_answer": correct_answer,
            "wrong_answer": wrong_answer,
            "is_absend": is_absend,
        }
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def batch_update_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Batch update student results.
        results: [
            {"item_id": 1, "marks": 45.5, "correct_answer": 50, "wrong_answer": 18, "is_absend": 0},
            ...
        ]
        """
        url = f"{self.base_url}/batch"
        payload = {"results": results}
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
