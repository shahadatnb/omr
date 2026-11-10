import os
import json
import tempfile
from pathlib import Path
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings

from omr_engine.sheet_processor import process_sheet
from integration.laravel_client import LaravelOmrClient

class HealthCheckView(APIView):
    def get(self, request):
        return Response({"status": "healthy", "service": "OMR Engine API"})


class ProcessSheetView(APIView):
    """
    POST /api/process/
    Uploads an image, processes OMR, and optionally matches with Laravel.
    """
    def post(self, request):
        if 'image' not in request.FILES:
            return Response({'error': 'Image file is required'}, status=status.HTTP_400_BAD_REQUEST)

        image_file = request.FILES['image']
        class_test_id = request.data.get('class_test_id')
        template_name = request.data.get('template_name', 'admission_challenge_100q.json')
        template_path = settings.OMR_TEMPLATES_DIR / template_name

        if not template_path.exists():
            return Response({'error': f'Template not found: {template_name}'}, status=status.HTTP_404_NOT_FOUND)

        # Save to temp file
        ext = os.path.splitext(image_file.name)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            for chunk in image_file.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        try:
            answer_key = None
            client = LaravelOmrClient(base_url=settings.LARAVEL_API_URL)

            # First pass: process sheet to detect Set & answers
            result = process_sheet(tmp_path, template_path, answer_key=None, debug=True)

            # If class_test_id and set detected, fetch key from Laravel and score
            if class_test_id and result.get('exam_set'):
                try:
                    ak_data = client.get_answer_key(int(class_test_id), result['exam_set'])
                    if ak_data and 'answers' in ak_data:
                        from omr_engine.scorer import calculate_score
                        result['score'] = calculate_score(
                            result['answers'],
                            ak_data['answers'],
                            correct_mark=ak_data.get('correct_mark', 1.0),
                            wrong_mark=ak_data.get('wrong_mark', -0.25)
                        )
                except Exception as e:
                    result['score_error'] = str(e)

            # If class_test_id and numeric_id detected, find student
            student_info = None
            if class_test_id and result.get('student_numeric_id'):
                try:
                    student_info = client.find_student(int(class_test_id), result['student_numeric_id'])
                except Exception as e:
                    result['student_lookup_error'] = str(e)

            result['student_info'] = student_info
            return Response(result)

        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
