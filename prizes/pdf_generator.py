"""
prizes/pdf_generator.py
------------------------
Génération de certificats PDF avec ReportLab.
Utilise uniquement les données réelles de la base de données.
"""
import io
from xml.sax.saxutils import escape
from django.utils import timezone


# Palette de couleurs (reprend le thème CodeStorm)
COLOR_PRIMARY = '#533DCF'
COLOR_ON_SURFACE = '#161A30'
COLOR_OUTLINE = '#797586'
COLOR_GOLD = '#C8A84B'


def generate_certificate_pdf(certificate) -> bytes:
    """
    Génère un certificat PDF à partir d'un objet Certificate.
    Retourne les octets du PDF.

    Paramètres :
        certificate : instance de prizes.models.Certificate
    Retourne :
        bytes : contenu du fichier PDF
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    )
    from reportlab.lib.enums import TA_CENTER

    color_primary = colors.HexColor(COLOR_PRIMARY)
    color_on_surface = colors.HexColor(COLOR_ON_SURFACE)
    color_outline = colors.HexColor(COLOR_OUTLINE)
    color_gold = colors.HexColor(COLOR_GOLD)
    buffer = io.BytesIO()

    # Format paysage A4
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    story = []
    styles = getSampleStyleSheet()

    # --- Styles personnalisés ---
    title_style = ParagraphStyle(
        'CertTitle',
        parent=styles['Title'],
        fontSize=11,
        textColor=color_outline,
        alignment=TA_CENTER,
        spaceAfter=0,
        fontName='Helvetica',
        letterSpacing=3,
    )
    recipient_style = ParagraphStyle(
        'RecipientName',
        parent=styles['Title'],
        fontSize=34,
        textColor=color_primary,
        alignment=TA_CENTER,
        spaceBefore=4,
        spaceAfter=4,
        fontName='Helvetica-Bold',
    )
    body_style = ParagraphStyle(
        'CertBody',
        parent=styles['Normal'],
        fontSize=12,
        textColor=color_on_surface,
        alignment=TA_CENTER,
        spaceAfter=4,
        fontName='Helvetica',
    )
    hackathon_style = ParagraphStyle(
        'HackathonName',
        parent=styles['Heading2'],
        fontSize=18,
        textColor=color_on_surface,
        alignment=TA_CENTER,
        spaceBefore=4,
        spaceAfter=6,
        fontName='Helvetica-Bold',
    )
    award_style = ParagraphStyle(
        'AwardBadge',
        parent=styles['Normal'],
        fontSize=14,
        textColor=color_gold,
        alignment=TA_CENTER,
        spaceBefore=6,
        spaceAfter=12,
        fontName='Helvetica-Bold',
    )
    uid_style = ParagraphStyle(
        'UID',
        parent=styles['Normal'],
        fontSize=8,
        textColor=color_outline,
        alignment=TA_CENTER,
        fontName='Courier',
    )
    footer_label_style = ParagraphStyle(
        'FooterLabel',
        parent=styles['Normal'],
        fontSize=9,
        textColor=color_outline,
        alignment=TA_CENTER,
        fontName='Helvetica',
    )
    footer_name_style = ParagraphStyle(
        'FooterName',
        parent=styles['Normal'],
        fontSize=11,
        textColor=color_on_surface,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
    )

    # --- En-tête : plateforme + UID ---
    story.append(Paragraph('CODESTORM PLATFORM', title_style))
    story.append(Spacer(1, 0.2 * cm))
    story.append(HRFlowable(
        width='100%', thickness=1.5,
        color=color_primary, spaceAfter=0.5 * cm
    ))

    # --- Corps du certificat ---
    story.append(Paragraph('CERTIFICATE OF ACHIEVEMENT', title_style))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph('This official document is proudly conferred upon', body_style))

    # Nom du bénéficiaire
    recipient_name = escape(
        certificate.recipient.get_full_name()
        or certificate.recipient.username
    )
    story.append(Paragraph(recipient_name, recipient_style))

    story.append(HRFlowable(
        width='40%', thickness=0.5,
        color=color_primary, hAlign='CENTER', spaceAfter=0.4 * cm
    ))

    story.append(Paragraph(
        'for outstanding achievement and participation in',
        body_style
    ))

    # Nom du hackathon
    story.append(Paragraph(escape(certificate.hackathon.name), hackathon_style))

    # Prix / type de certificat
    award_text = ''
    if certificate.prize:
        award_text = escape(certificate.prize.name)
        if certificate.prize.amount:
            award_text += f'  —  ${certificate.prize.amount:,.2f} USD'
    elif certificate.cert_type:
        award_text = certificate.get_cert_type_display()
    if certificate.rank:
        award_text = f'Rank #{certificate.rank}  ·  {award_text}'

    story.append(Paragraph(award_text, award_style))

    # Date de délivrance
    issued_date = timezone.localdate(certificate.issued_at).strftime('%B %d, %Y')
    story.append(Paragraph(f'Issued on {issued_date}', body_style))

    story.append(Spacer(1, 0.8 * cm))
    story.append(HRFlowable(
        width='100%', thickness=0.5,
        color=color_outline, spaceAfter=0.5 * cm
    ))

    # --- Pied de page : organisateur + sceau + UID ---
    organizer_name = escape(
        certificate.hackathon.organizer.get_full_name()
        or certificate.hackathon.organizer.username
    )

    footer_data = [
        [
            Paragraph(organizer_name, footer_name_style),
            Paragraph('✦', ParagraphStyle(
                'Seal', parent=styles['Normal'],
                fontSize=36, textColor=color_primary,
                alignment=TA_CENTER, fontName='Helvetica'
            )),
            Paragraph(
                f'ID: HF-{certificate.short_uid}',
                uid_style
            ),
        ],
        [
            Paragraph('Lead Organizer', footer_label_style),
            Paragraph('CodeStorm Official', footer_label_style),
            Paragraph('Verification Code', footer_label_style),
        ],
    ]

    footer_table = Table(footer_data, colWidths=['33%', '34%', '33%'])
    footer_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(footer_table)

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
