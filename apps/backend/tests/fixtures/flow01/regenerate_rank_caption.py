"""Rebuild the embedded-font PDF with ReportLab 4.4.9 and the shipped Inter font."""
from pathlib import Path
import sys

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from test_flow01_04_postgres import profit_page, rank_page, site_page


def generate(output):
    backend = Path(__file__).resolve().parents[3]
    font = backend / 'assets' / 'aso' / 'Inter-Regular.ttf'
    pdfmetrics.registerFont(TTFont('Inter', font))
    document = canvas.Canvas(str(output), pagesize=(842, 595), invariant=1, pageCompression=0)
    document.setTitle('Synthetic rank and production cards')
    document.setAuthor('AIST')
    for page in (rank_page(), site_page(), profit_page()):
        document.setFont('Inter', 12)
        for text, x, y in page:
            document.drawString(x, y, text)
        document.showPage()
    document.save()


if __name__ == '__main__':
    generate(Path(sys.argv[1]))
