from django.db import models
from django.utils import timezone


class BatchJob(models.Model):
    class_test_id = models.IntegerField(help_text="Laravel ClassTest ID")
    exam_title = models.CharField(max_length=255, blank=True, null=True)
    template_name = models.CharField(max_length=120, default='admission_challenge_100q.json', help_text="OMR Template File")
    created_at = models.DateTimeField(default=timezone.now)
    total_files = models.IntegerField(default=0)
    processed_count = models.IntegerField(default=0)
    matched_count = models.IntegerField(default=0)
    unmatched_count = models.IntegerField(default=0)
    synced_to_laravel = models.BooleanField(default=False)
    synced_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']

    def update_stats(self):
        """Update and recalculate batch statistics."""
        self.total_files = self.results.count()
        self.processed_count = self.results.exclude(status='pending').count()
        self.matched_count = self.results.filter(is_matched=True).count()
        self.unmatched_count = self.results.filter(is_matched=False).exclude(status='pending').count()
        self.save(update_fields=['total_files', 'processed_count', 'matched_count', 'unmatched_count'])

    def __str__(self):
        return f"Batch #{self.id} - Test #{self.class_test_id} ({self.created_at.strftime('%Y-%m-%d %H:%M')})"


class ScanResult(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending Processing'),
        ('matched', 'Matched'),
        ('unmatched', 'Unmatched'),
        ('manual', 'Manually Edited'),
        ('error', 'Processing Error'),
    )

    batch = models.ForeignKey(BatchJob, on_delete=models.CASCADE, related_name='results')
    image_file = models.FileField(upload_to='scans/%Y/%m/%d/')
    original_filename = models.CharField(max_length=255)
    
    # OMR Extracted values
    student_numeric_id = models.CharField(max_length=30, blank=True, null=True)
    exam_set = models.CharField(max_length=5, blank=True, null=True)
    answers_json = models.JSONField(default=dict, blank=True)
    
    # Scoring
    marks = models.FloatField(default=0.0)
    correct_count = models.IntegerField(default=0)
    wrong_count = models.IntegerField(default=0)
    unattempted_count = models.IntegerField(default=0)

    # Laravel Student Matching
    is_matched = models.BooleanField(default=False)
    is_manually_edited = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unmatched')
    item_id = models.IntegerField(blank=True, null=True, help_text="Laravel class_test_items.id")
    student_id = models.IntegerField(blank=True, null=True, help_text="Laravel students.id")
    student_name = models.CharField(max_length=255, blank=True, null=True)
    student_reg_no = models.CharField(max_length=100, blank=True, null=True)

    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-is_matched', 'original_filename']

    def __str__(self):
        return f"{self.original_filename} - ID: {self.student_numeric_id or 'N/A'} - Set: {self.exam_set or 'N/A'}"


class StandaloneExam(models.Model):
    """
    Offline/Standalone Exam that operates completely independently of Laravel API.
    Answer keys are created and stored directly in SQLite, and evaluated by Roll No from OMR sheet.
    """
    title = models.CharField(max_length=255, help_text="পরীক্ষার নাম (যেমন: মডেল টেস্ট ০১)")
    exam_date = models.DateField(default=timezone.now)
    template_name = models.CharField(max_length=120, default='offline_exam_100q.json', help_text="OMR Template File")
    total_questions = models.IntegerField(default=100)
    correct_mark = models.FloatField(default=1.0, help_text="প্রতিটি সঠিক উত্তরের জন্য প্রাপ্ত নম্বর")
    wrong_mark = models.FloatField(default=-0.25, help_text="প্রতিটি ভুল উত্তরের জন্য কাটা নম্বর")

    # Local Answer Keys for sets A, B, C, D
    answers_set_a = models.JSONField(default=dict, blank=True)
    answers_set_b = models.JSONField(default=dict, blank=True)
    answers_set_c = models.JSONField(default=dict, blank=True)
    answers_set_d = models.JSONField(default=dict, blank=True)

    total_sheets = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def get_answer_key(self, set_name):
        set_name = (set_name or 'A').upper()
        if set_name == 'A':
            return self.answers_set_a or {}
        elif set_name == 'B':
            return self.answers_set_b or {}
        elif set_name == 'C':
            return self.answers_set_c or {}
        elif set_name == 'D':
            return self.answers_set_d or {}
        return {}

    def recalculate_ranks(self):
        """Sort results by marks desc and assign merit rank using fast bulk_update."""
        results = list(self.results.all().order_by('-marks', 'unattempted_count'))
        if not results:
            self.total_sheets = 0
            self.save(update_fields=['total_sheets'])
            return

        current_rank = 1
        for res in results:
            res.rank = current_rank
            current_rank += 1

        StandaloneResult.objects.bulk_update(results, ['rank'], batch_size=500)
        self.total_sheets = len(results)
        self.save(update_fields=['total_sheets'])

    def __str__(self):
        return f"{self.title} ({self.created_at.strftime('%d %b %Y')})"


class StandaloneResult(models.Model):
    """
    Results evaluated directly from OMR sheet roll number without external API.
    """
    exam = models.ForeignKey(StandaloneExam, on_delete=models.CASCADE, related_name='results')
    roll_no = models.CharField(max_length=30, help_text="ওএমআর শিট থেকে শনাক্তকৃত রোল নম্বর")
    exam_set = models.CharField(max_length=5, default='A')
    image_file = models.FileField(upload_to='standalone_scans/%Y/%m/%d/', blank=True, null=True)
    original_filename = models.CharField(max_length=255)
    
    answers_json = models.JSONField(default=dict, blank=True)
    marks = models.FloatField(default=0.0)
    correct_count = models.IntegerField(default=0)
    wrong_count = models.IntegerField(default=0)
    unattempted_count = models.IntegerField(default=0)
    rank = models.IntegerField(default=0)
    student_name = models.CharField(max_length=150, blank=True, default='', help_text="শিক্ষার্থীর নাম (ঐচ্ছিক)")
    phone_number = models.CharField(max_length=30, blank=True, default='', help_text="মোবাইল নম্বর (ওএমআর বাবল বা এক্সেল থেকে)")
    sms_status = models.CharField(max_length=20, default='none', choices=[
        ('none', 'Not Sent'),
        ('sent', 'Sent'),
        ('failed', 'Failed')
    ])
    sms_sent_at = models.DateTimeField(null=True, blank=True)
    sms_error = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['rank', '-marks', 'roll_no']

    def __str__(self):
        return f"Roll: {self.roll_no} | Score: {self.marks} | Rank: {self.rank}"


class InstituteSettings(models.Model):
    """
    Settings for Institution Name, Address, Contact, and Logo for printing reports.
    """
    institute_name = models.CharField(max_length=255, default="Admission Challenge", help_text="প্রতিষ্ঠানের নাম")
    tagline = models.CharField(max_length=255, blank=True, null=True, default="Advanced OMR Evaluation System", help_text="স্লোগান বা উপ-শিরোনাম")
    address = models.TextField(blank=True, null=True, default="ঢাকা, বাংলাদেশ", help_text="প্রতিষ্ঠানের পূর্ণাঙ্গ ঠিকানা")
    phone = models.CharField(max_length=100, blank=True, null=True, default="+880 1700-000000", help_text="ফোন বা হেল্পলাইন নম্বর")
    email = models.EmailField(blank=True, null=True, default="info@admissionchallenge.com")
    website = models.CharField(max_length=150, blank=True, null=True, default="www.admissionchallenge.com")
    logo = models.ImageField(upload_to='settings/', blank=True, null=True, help_text="প্রতিষ্ঠানের লোগো")
    print_footer_note = models.CharField(
        max_length=255,
        default="এটি একটি কম্পিউটার জেনারেটেড ওএমআর মেধা তালিকা, এতে কোনো প্রকার কাটাকাটি গ্রহণযোগ্য নয়।",
        help_text="প্রিন্ট পেজের নিচের ফুটনোট"
    )

    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return self.institute_name


class ModemSettings(models.Model):
    """
    Settings for USB GSM Modem (Port, Baud rate, Delay, SMS Template).
    """
    com_port = models.CharField(max_length=50, blank=True, default='', help_text="মডেমের COM পোর্ট যেমন: COM3, COM4")
    baud_rate = models.IntegerField(default=115200, help_text="Baud Rate (ডিফল্ট: 115200 বা 9600)")
    delay_seconds = models.FloatField(default=2.5, help_text="প্রতিটি এসএমএস পাঠানোর মধ্যবর্তী বিরতি (সেকেন্ড)")
    is_enabled = models.BooleanField(default=True, help_text="মডেম এসএমএস সক্রিয় আছে কিনা")
    sms_template = models.TextField(
        default="[INSTITUTE_NAME]\nExam: [EXAM_TITLE]\nMob/Roll: [ROLL]\nScore: [MARKS]/[TOTAL_Q] (Rank: [RANK])\nCorrect: [CORRECT], Wrong: [WRONG]",
        help_text="এসএমএস টেমপ্লেট"
    )

    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"Modem: {self.com_port or 'Unset'} ({self.baud_rate})"


