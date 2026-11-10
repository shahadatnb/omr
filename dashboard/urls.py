from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.batch_list, name='batch_list'),
    path('upload/', views.batch_upload, name='batch_upload'),
    path('batch/<int:batch_id>/', views.batch_detail, name='batch_detail'),
    path('batch/<int:batch_id>/sync/', views.sync_to_laravel, name='sync_to_laravel'),
    path('batch/<int:batch_id>/delete/', views.batch_delete, name='batch_delete'),
    path('result/<int:result_id>/edit/', views.manual_edit, name='manual_edit'),
    path('result/<int:result_id>/delete/', views.scan_result_delete, name='scan_result_delete'),

    # Template Editor routes
    path('template-editor/', views.template_editor, name='template_editor'),
    path('template-editor/test-ajax/', views.template_test_ajax, name='template_test_ajax'),
    path('template-editor/upload-sample-ajax/', views.template_upload_sample_ajax, name='template_upload_sample_ajax'),

    # AJAX Endpoints for real-time progress & appending to batches
    path('batch/create-ajax/', views.batch_create_ajax, name='batch_create_ajax'),
    path('batch/<int:batch_id>/upload-single-ajax/', views.upload_single_ajax, name='upload_single_ajax'),
    path('batch/<int:batch_id>/scan-folder-ajax/', views.scan_folder_ajax, name='scan_folder_ajax'),
    path('batch/<int:batch_id>/process-item-ajax/<int:scan_id>/', views.process_item_ajax, name='process_item_ajax'),

    # Global Institution Settings (for print header/footer)
    path('settings/', views.institute_settings_view, name='institute_settings'),

    # STANDALONE OFFLINE EXAM SYSTEM (NO API DEPENDENCY)
    path('standalone/', views.standalone_list, name='standalone_list'),
    path('standalone/create/', views.standalone_create, name='standalone_create'),
    path('standalone/<int:exam_id>/keys/', views.standalone_answer_key, name='standalone_answer_key'),
    path('standalone/<int:exam_id>/upload/', views.standalone_upload, name='standalone_upload'),
    path('standalone/<int:exam_id>/upload-single-ajax/', views.standalone_upload_single_ajax, name='standalone_upload_single_ajax'),
    path('standalone/<int:exam_id>/finalize-ranks-ajax/', views.standalone_finalize_ranks_ajax, name='standalone_finalize_ranks_ajax'),
    path('standalone/<int:exam_id>/results/', views.standalone_results, name='standalone_results'),
    path('standalone/<int:exam_id>/print/', views.standalone_print, name='standalone_print'),
    path('standalone/<int:exam_id>/export/', views.standalone_export_csv, name='standalone_export_csv'),
    path('standalone/<int:exam_id>/delete/', views.standalone_delete, name='standalone_delete'),
    path('standalone/result/<int:result_id>/delete/', views.standalone_result_delete, name='standalone_result_delete'),
    path('standalone/result/<int:result_id>/detail-ajax/', views.standalone_result_detail_ajax, name='standalone_result_detail_ajax'),
]
