"""Repair records: the close-out record as a PDF repair report and a trainee assessment sheet
(HTML template → headless Chromium, ARCHITECTURE.md §8), and recall over past records."""
from .export import ExportUnavailable, RecordView, assessment_html, load_record, recall, report_html, to_pdf

__all__ = ["ExportUnavailable", "RecordView", "assessment_html", "load_record", "recall", "report_html", "to_pdf"]
