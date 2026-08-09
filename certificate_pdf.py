"""
Certificate PDF rendering - the only file that touches reportlab/qrcode.
Kept separate from webserver.py so the routing stays simple: it just
calls build_certificate_pdf() and streams the result back.
"""
import io


def make_qr_png(data: str) -> bytes:
    """Returns raw PNG bytes for a QR code encoding `data` (a verify URL)."""
    import qrcode  # imported lazily so the app still boots fine without the package installed

    qr = qrcode.QRCode(border=1, box_size=8)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#2B2320", back_color="#FBF3E3")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_certificate_pdf(certificate, learner_display_name: str, verify_url: str) -> io.BytesIO:
    """
    Renders one certificate as a landscape PDF and returns it as an
    in-memory buffer (never written to disk - each download is generated
    fresh from the DB row, so there's nothing stale to clean up).
    """
    from reportlab.lib.colors import HexColor  # imported lazily so the app still boots fine without the package installed
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    width, height = landscape(A4)
    c = canvas.Canvas(buffer, pagesize=(width, height))

    cream = HexColor("#FBF3E3")
    ink = HexColor("#2B2320")
    gold = HexColor("#B8892E")
    accent = HexColor("#4B3AA8")  # print-friendly stand-in for the site's neon purple

    # Background + double border frame
    c.setFillColor(cream)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    margin = 16 * mm
    c.setStrokeColor(gold)
    c.setLineWidth(2)
    c.rect(margin, margin, width - 2 * margin, height - 2 * margin, fill=0, stroke=1)
    c.setLineWidth(0.6)
    c.rect(margin + 4 * mm, margin + 4 * mm, width - 2 * margin - 8 * mm, height - 2 * margin - 8 * mm, fill=0, stroke=1)

    # Header
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width / 2, height - margin - 16 * mm, "Coder Enchanté Academy")

    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(width / 2, height - margin - 32 * mm, "Certificate of Completion")

    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - margin - 43 * mm, "This is to certify that")

    c.setFillColor(accent)
    c.setFont("Times-BoldItalic", 28)
    c.drawCentredString(width / 2, height - margin - 57 * mm, learner_display_name)

    c.setFillColor(ink)
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - margin - 68 * mm, "has successfully completed the course")

    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(width / 2, height - margin - 79 * mm, certificate.title)

    c.setFont("Helvetica", 10.5)
    c.drawCentredString(width / 2, height - margin - 88 * mm, "with all projects and assessments.")

    # Bottom-left: certificate ID + date
    c.setFont("Helvetica", 9.5)
    c.setFillColor(ink)
    c.drawString(margin + 12 * mm, margin + 18 * mm, f"Certificate ID: {certificate.certificate_uid}")
    c.drawString(margin + 12 * mm, margin + 12 * mm, f"Date: {certificate.issued_at.strftime('%d %B %Y')}")

    # Bottom-right: QR code (scan to verify) + signature line
    qr_bytes = make_qr_png(verify_url)
    qr_size = 24 * mm
    qr_x = width - margin - 14 * mm - qr_size
    qr_y = margin + 9 * mm
    c.drawImage(ImageReader(io.BytesIO(qr_bytes)), qr_x, qr_y, width=qr_size, height=qr_size)
    c.setFont("Helvetica", 6.5)
    c.drawCentredString(qr_x + qr_size / 2, qr_y - 8, "Scan to verify")

    sig_center_x = width - margin - 46 * mm
    c.setStrokeColor(ink)
    c.setLineWidth(0.6)
    c.line(sig_center_x - 26 * mm, margin + 22 * mm, sig_center_x + 26 * mm, margin + 22 * mm)
    c.setFont("Helvetica-Oblique", 10)
    c.drawCentredString(sig_center_x, margin + 24 * mm, "Coder Enchanté Academy")
    c.setFont("Helvetica", 8)
    c.drawCentredString(sig_center_x, margin + 17 * mm, "Instructor")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer
