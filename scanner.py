import azure.functions as func
import logging
import json
import os
import uuid
import base64
from datetime import datetime, timedelta
from io import BytesIO

# Import OpenAI
try:
    from openai import AzureOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logging.warning("OpenAI package not available")

# Import Azure Storage
try:
    from azure.storage.blob import BlobServiceClient, ContentSettings
    STORAGE_AVAILABLE = True
except ImportError:
    STORAGE_AVAILABLE = False
    logging.warning("Azure Storage package not available")

# Import Cosmos DB
try:
    from azure.cosmos import CosmosClient, exceptions
    COSMOS_AVAILABLE = True
except ImportError:
    COSMOS_AVAILABLE = False
    logging.warning("Azure Cosmos package not available")

# Import ReportLab for PDF generation
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm, cm
    from reportlab.lib.colors import HexColor, black, white
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    logging.warning("ReportLab package not available")

# Import PIL for image handling
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("PIL package not available")

# Import Azure Communication Services for email
try:
    from azure.communication.email import EmailClient
    EMAIL_AVAILABLE = True
except ImportError:
    EMAIL_AVAILABLE = False
    logging.warning("Azure Communication Email package not available")

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# =============================================================================
# HELPER FUNCTIONS - Storage
# =============================================================================

def get_blob_service_client():
    conn_str = os.environ.get("STORAGE_CONNECTION_STRING")
    if not conn_str:
        return None
    return BlobServiceClient.from_connection_string(conn_str)


def upload_image_to_blob(assessment_id: str, image_name: str, image_data: bytes) -> str:
    blob_service = get_blob_service_client()
    if not blob_service:
        raise Exception("Storage connection string not configured")
    container_client = blob_service.get_container_client("uploads")
    blob_name = f"{assessment_id}/{image_name}.png"
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(image_data, overwrite=True)
    return blob_client.url


def upload_pdf_to_blob(assessment_id: str, pdf_data: bytes) -> str:
    blob_service = get_blob_service_client()
    if not blob_service:
        raise Exception("Storage connection string not configured")
    container_client = blob_service.get_container_client("reports")
    blob_name = f"{assessment_id}/report.pdf"
    blob_client = container_client.get_blob_client(blob_name)
    content_settings = ContentSettings(content_type='application/pdf')
    blob_client.upload_blob(pdf_data, overwrite=True, content_settings=content_settings)
    return blob_client.url


def get_pdf_from_blob(assessment_id: str) -> bytes:
    blob_service = get_blob_service_client()
    if not blob_service:
        raise Exception("Storage connection string not configured")
    container_client = blob_service.get_container_client("reports")
    blob_name = f"{assessment_id}/report.pdf"
    blob_client = container_client.get_blob_client(blob_name)
    return blob_client.download_blob().readall()


def get_logo_from_blob() -> bytes:
    blob_service = get_blob_service_client()
    if not blob_service:
        return None
    try:
        container_client = blob_service.get_container_client("uploads")
        blob_client = container_client.get_blob_client("logo.png")
        return blob_client.download_blob().readall()
    except Exception:
        return None


# =============================================================================
# HELPER FUNCTIONS - Cosmos DB
# =============================================================================

def get_cosmos_container():
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("Assessments")
    return container


def save_assessment_to_db(assessment_data: dict, ttl_days: int = 7) -> dict:
    container = get_cosmos_container()
    if not container:
        raise Exception("Cosmos DB connection string not configured")
    data_to_save = assessment_data.copy()
    if "id" not in data_to_save:
        data_to_save["id"] = data_to_save.get("assessment_id", str(uuid.uuid4()))
    data_to_save["assessment_id"] = data_to_save["id"]
    data_to_save["created_at"] = datetime.utcnow().isoformat()
    data_to_save["updated_at"] = datetime.utcnow().isoformat()
    data_to_save["ttl"] = ttl_days * 24 * 60 * 60
    # Privacy: replace child's name before storing (email kept for 7-day customer lookup)
    if "child" in data_to_save:
        data_to_save["child"]["name"] = "Child"
    result = container.upsert_item(body=data_to_save)
    return result


def get_assessment_from_db(assessment_id: str) -> dict:
    container = get_cosmos_container()
    if not container:
        raise Exception("Cosmos DB connection string not configured")
    try:
        item = container.read_item(item=assessment_id, partition_key=assessment_id)
        return item
    except exceptions.CosmosResourceNotFoundError:
        return None

def get_admin_stats_container():
    """Get the AdminStats container - stores anonymised records permanently."""
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("AdminStats")
    return container


def get_refund_lookup_container():
    """Get the RefundLookup container - stores email + assessment_id for 14 days."""
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("RefundLookup")
    return container


def get_age_band(age_months: int) -> str:
    """Convert age in months to anonymised age band."""
    if age_months < 30:
        return "2-2.5"
    elif age_months < 36:
        return "2.5-3"
    elif age_months < 42:
        return "3-3.5"
    else:
        return "3.5+"


def save_admin_stat(assessment_data: dict):
    """Save an anonymised record to AdminStats. No name, no email, no images."""
    try:
        container = get_admin_stats_container()
        if not container:
            logging.warning("AdminStats container not available")
            return
        
        assessment_id = assessment_data.get("assessment_id", str(uuid.uuid4()))
        age_months = assessment_data.get("child", {}).get("age_months", 0)
        scoring = assessment_data.get("scoring", {})
        interpretation = assessment_data.get("interpretation", {})
        
        stat_record = {
            "id": assessment_id,
            "assessment_id": assessment_id,
            "product": "starter",
            "created_at": datetime.utcnow().isoformat(),
            "age_band": get_age_band(age_months),
            "stage": interpretation.get("stage", "UNKNOWN"),
            "writing_stage": interpretation.get("writing_stage", "UNKNOWN"),
            "score_percentage": scoring.get("percentage", 0),
            "total_score": scoring.get("total_score", 0),
            "max_score": scoring.get("max_score", 0),
            "pairs_completed": assessment_data.get("pairs_completed", {}),
            "partial_assessment": assessment_data.get("partial_assessment", False),
            "email_sent": assessment_data.get("email_sent", False),
            "status": "completed",
            "is_test": assessment_data.get("is_test", False),
            "refunded": False
        }
        
        container.upsert_item(body=stat_record)
        logging.info(f"Admin stat saved for {assessment_id}")
    except Exception as e:
        logging.error(f"Failed to save admin stat: {str(e)}")


def save_refund_lookup(assessment_id: str, email: str):
    """Save email + assessment_id for refund lookup. 14-day TTL."""
    try:
        container = get_refund_lookup_container()
        if not container:
            logging.warning("RefundLookup container not available")
            return
        
        record = {
            "id": assessment_id,
            "assessment_id": assessment_id,
            "email": email,
            "created_at": datetime.utcnow().isoformat(),
            "refunded": False,
            "ttl": 14 * 24 * 60 * 60
        }
        
        container.upsert_item(body=record)
        logging.info(f"Refund lookup saved for {assessment_id}")
    except Exception as e:
        logging.error(f"Failed to save refund lookup: {str(e)}")


def verify_admin_password(req: func.HttpRequest) -> bool:
    """Check the admin password from request header or query param."""
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_password:
        logging.error("ADMIN_PASSWORD not set in app settings")
        return False
    
    provided = req.headers.get("X-Admin-Password", "")
    if not provided:
        provided = req.params.get("admin_password", "")
    
    return provided == admin_password


# =============================================================================
# EMAIL FUNCTIONS
# =============================================================================

def get_email_client():
    conn_str = os.environ.get("EMAIL_CONNECTION_STRING")
    if not conn_str:
        return None
    return EmailClient.from_connection_string(conn_str)


def send_assessment_email(recipient_email: str, child_name: str, assessment_id: str, pdf_bytes: bytes = None) -> dict:
    email_client = get_email_client()
    if not email_client:
        raise Exception("Email connection string not configured")
    
    subject = f"Early Writing Assessment Report for {child_name}"
    
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #1B75BC;">Early Writing Assessment Report</h1>
            <p>Dear Parent/Guardian,</p>
            <p>Thank you for completing the Early Writing Assessment for <strong>{child_name}</strong>.</p>
            <p>Please find attached the detailed assessment report.</p>
            <p>Best wishes,<br/><strong>The More Handwriting Team</strong></p>
            <hr style="border: none; border-top: 1px solid #ccc; margin: 30px 0;" />
            <p style="font-size: 12px; color: #666;">
                © More Handwriting | <a href="https://morehandwriting.co.uk">morehandwriting.co.uk</a>
            </p>
        </div>
    </body>
    </html>
    """
    
    message = {
        "senderAddress": "DoNotReply@morehandwriting.co.uk",
        "recipients": {"to": [{"address": recipient_email}]},
        "content": {"subject": subject, "html": html_content}
    }
    
    if pdf_bytes:
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        message["attachments"] = [{
            "name": f"Assessment_Report_{child_name}.pdf",
            "contentType": "application/pdf",
            "contentInBase64": pdf_base64
        }]
    
    poller = email_client.begin_send(message)
    result = poller.result()
    return {"message_id": result.get("id", ""), "status": "sent"}


# =============================================================================
# PDF GENERATION
# =============================================================================

def generate_assessment_pdf(assessment_data: dict) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise Exception("ReportLab not available")
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    
    # Calculate available width for content
    page_width = A4[0]  # 595 points
    available_width = page_width - 4*cm  # ~480 points
    
    # Colours
    primary_blue = HexColor('#1B75BC')
    light_blue = HexColor('#E8F4FC')
    light_grey = HexColor('#F5F5F5')
    dark_grey = HexColor('#666666')
    medium_grey = HexColor('#888888')
    success_green = HexColor('#28A745')
    light_green = HexColor('#E8F8E8')
    warning_amber = HexColor('#F5A623')
    light_amber = HexColor('#FFF9E6')
    info_blue = HexColor('#17A2B8')
    light_info = HexColor('#E3F6F9')
    highlight_bg = HexColor('#FFFBF0')
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=22, textColor=primary_blue, spaceAfter=6, alignment=TA_CENTER)
    subtitle_style = ParagraphStyle('CustomSubtitle', parent=styles['Normal'], fontSize=13, textColor=dark_grey, spaceAfter=4, alignment=TA_CENTER)
    meta_style = ParagraphStyle('MetaStyle', parent=styles['Normal'], fontSize=9, textColor=medium_grey, spaceAfter=3, alignment=TA_CENTER)
    heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading2'], fontSize=14, textColor=primary_blue, spaceBefore=15, spaceAfter=8)
    subheading_style = ParagraphStyle('CustomSubheading', parent=styles['Heading3'], fontSize=11, textColor=dark_grey, spaceBefore=10, spaceAfter=4, fontName='Helvetica-Bold')
    body_style = ParagraphStyle('CustomBody', parent=styles['Normal'], fontSize=10, textColor=black, spaceAfter=6, leading=14)
    small_style = ParagraphStyle('CustomSmall', parent=styles['Normal'], fontSize=8, textColor=medium_grey, spaceAfter=4)
    bullet_style = ParagraphStyle('BulletStyle', parent=styles['Normal'], fontSize=10, textColor=dark_grey, spaceAfter=4, leftIndent=15, leading=14)
    observation_style = ParagraphStyle('ObsStyle', parent=styles['Normal'], fontSize=9, textColor=dark_grey, spaceAfter=8, leading=13)
    activity_title_style = ParagraphStyle('ActivityTitle', parent=styles['Normal'], fontSize=11, textColor=primary_blue, spaceBefore=10, spaceAfter=3, fontName='Helvetica-Bold')
    activity_body_style = ParagraphStyle('ActivityBody', parent=styles['Normal'], fontSize=9, textColor=dark_grey, spaceAfter=8, leading=13, leftIndent=0)
    
    story = []
    
    # Logo
    logo_data = get_logo_from_blob()
    if logo_data and PIL_AVAILABLE:
        try:
            logo_image = PILImage.open(BytesIO(logo_data))
            logo_width = 130
            aspect = logo_image.height / logo_image.width
            logo_height = logo_width * aspect
            logo_buffer = BytesIO()
            logo_image.save(logo_buffer, format='PNG')
            logo_buffer.seek(0)
            img = Image(logo_buffer, width=logo_width, height=logo_height)
            story.append(img)
            story.append(Spacer(1, 8))
        except Exception as e:
            logging.warning(f"Could not add logo: {e}")
    
    # Header
    story.append(Paragraph("Early Writing Starter Report", title_style))
    
    child = assessment_data.get('child', {})
    child_name = child.get('name', 'Unknown')
    age_display = child.get('age_display', '')
    child_age_months = child.get('age_months', 36)
    
    story.append(Paragraph(f"For {child_name}", subtitle_style))
    story.append(Paragraph(f"Age: {age_display}  •  {datetime.now().strftime('%d %B %Y')}", meta_style))
    
    if assessment_data.get('partial_assessment'):
        story.append(Spacer(1, 6))
        story.append(Paragraph("<i>Based on one pair of samples</i>", meta_style))
    
    # Disclaimer
    story.append(Spacer(1, 20))
    disclaimer_style = ParagraphStyle('Disclaimer', parent=styles['Normal'], fontSize=8, textColor=medium_grey, alignment=TA_CENTER, leading=10)
    story.append(Paragraph("This assessment provides guidance based on research, not a definitive evaluation. If you have concerns about your child's development, consult a qualified professional.", disclaimer_style))
    
    story.append(Spacer(1, 15))
    
    # Get interpretation data
    interpretation = assessment_data.get('interpretation', {})
    stage = interpretation.get('stage', 'Unknown')
    
    # Stage styling
    if stage == "ALREADY WRITING":
        stage_color = success_green
        stage_bg = light_green
        stage_display = "Already Writing!"
    elif stage == "STRONG START":
        stage_color = success_green
        stage_bg = light_green
        stage_display = "Strong Start"
    elif stage == "BEGINNING EXPLORER":
        stage_color = warning_amber
        stage_bg = light_amber
        stage_display = "Beginning Explorer"
    else:
        stage_color = info_blue
        stage_bg = light_info
        stage_display = "Early Days"
    
    # Stage box - NO bullet point, full width to match text
    stage_title_style = ParagraphStyle('StageTitle', fontSize=18, textColor=stage_color, alignment=TA_CENTER, fontName='Helvetica-Bold')
    
    stage_content = [
        [Paragraph(stage_display, stage_title_style)],
    ]
    
    # Use available_width to match text alignment
    stage_table = Table(stage_content, colWidths=[available_width])
    stage_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), stage_bg),
        ('TOPPADDING', (0, 0), (-1, -1), 15),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 15),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(stage_table)
    
    story.append(Spacer(1, 12))

    # Short name explanation box (only shows if assessment was capped due to short name)
    if interpretation.get('short_name_capped', False):
        short_name_note_style = ParagraphStyle('ShortNameNote', fontSize=9, textColor=HexColor('#1B75BC'), leading=12)
        short_name_text = f"Note: Because {child_name}'s name has only 2-3 letters, we recommend completing the 'sun' writing task to fully assess their letter knowledge."
        short_name_content = [[Paragraph(short_name_text, short_name_note_style)]]
        short_name_table = Table(short_name_content, colWidths=[available_width])
        short_name_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#E8F4FD')),
            ('BOX', (0, 0), (-1, -1), 1, HexColor('#1B75BC')),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(short_name_table)
        story.append(Spacer(1, 12))
    
    # Get visual analysis data
    visual_analysis = assessment_data.get('visual_analysis', {})
    observations = visual_analysis.get('observations', {})
    pairs_completed = assessment_data.get('pairs_completed', {'pair1': True, 'pair2': True})
    
    # Development Journey Visual
    scoring = assessment_data.get('scoring', {})
    total_score = scoring.get('total_score', 0)
    max_score = scoring.get('max_score', 25)
    percentage = scoring.get('percentage', 0)
    
    journey_title_style = ParagraphStyle('JourneyTitle', fontSize=9, textColor=medium_grey, alignment=TA_CENTER, spaceAfter=8)
    story.append(Paragraph("Development Journey", journey_title_style))
    
    # Calculate column widths to match available_width
    col_width = available_width / 3
    
    # Journey stages labels
    # For ALREADY WRITING, highlight Strong Start since they have moved beyond it
    is_beyond_assessment = stage == "ALREADY WRITING"
    journey_labels = Table(
        [[
            Paragraph("Early Days", ParagraphStyle('JL', fontSize=8, textColor=info_blue if stage == "EARLY DAYS" else medium_grey, alignment=TA_LEFT)),
            Paragraph("Beginning Explorer", ParagraphStyle('JL', fontSize=8, textColor=warning_amber if stage == "BEGINNING EXPLORER" else medium_grey, alignment=TA_CENTER)),
            Paragraph("Strong Start", ParagraphStyle('JL', fontSize=8, textColor=success_green if (stage == "STRONG START" or is_beyond_assessment) else medium_grey, alignment=TA_RIGHT)),
        ]],
        colWidths=[col_width, col_width, col_width]
    )
    journey_labels.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(journey_labels)
    
    # Progress bar
    bar_height = 8
    bar_data = [['', '', '']]
    bar_table = Table(bar_data, colWidths=[col_width, col_width, col_width], rowHeights=[bar_height])
    
    if stage == "ALREADY WRITING" or stage == "STRONG START":
        # Both show all bars filled - ALREADY WRITING has moved beyond this scale
        bar_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), info_blue),
            ('BACKGROUND', (1, 0), (1, 0), warning_amber),
            ('BACKGROUND', (2, 0), (2, 0), success_green),
        ]))
    elif stage == "BEGINNING EXPLORER":
        bar_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), info_blue),
            ('BACKGROUND', (1, 0), (1, 0), warning_amber),
            ('BACKGROUND', (2, 0), (2, 0), light_grey),
        ]))
    else:  # EARLY DAYS
        bar_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), info_blue),
            ('BACKGROUND', (1, 0), (1, 0), light_grey),
            ('BACKGROUND', (2, 0), (2, 0), light_grey),
        ]))
    
    story.append(bar_table)
    story.append(Spacer(1, 15))
    
    # Comprehensive summary - detailed and age-contextualised
    favourite_colour_stated = assessment_data.get('questionnaire', {}).get('favourite_colour', '')
    comprehensive_summary = generate_comprehensive_summary(visual_analysis, child_name, stage, child_age_months, favourite_colour_stated)
    summary_style = ParagraphStyle('ComprehensiveSummary', fontSize=10, textColor=dark_grey, alignment=TA_LEFT, leading=15, spaceAfter=15)
    story.append(Paragraph(comprehensive_summary, summary_style))
    
    story.append(Spacer(1, 10))
    
    # What We Observed
    story.append(Paragraph("What We Observed", heading_style))
    
    obs_style = ParagraphStyle('ObsItem', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=6)
    
    if observations.get('name_writing'):
        story.append(Paragraph(f"<b>Name Writing:</b> {observations.get('name_writing')}", obs_style))
    if observations.get('self_portrait'):
        story.append(Paragraph(f"<b>Self Portrait:</b> {observations.get('self_portrait')}", obs_style))
    if observations.get('sun_writing'):
        story.append(Paragraph(f"<b>Sun Writing:</b> {observations.get('sun_writing')}", obs_style))
    if observations.get('sun_drawing'):
        story.append(Paragraph(f"<b>Sun Drawing:</b> {observations.get('sun_drawing')}", obs_style))
    
    story.append(Spacer(1, 15))

  # Metacognitive awareness insight - ONLY for children who CAN write but chose to draw
    pair2_both_drawings = assessment_data.get('scoring', {}).get('pair2_both_are_drawings', False)
    writing_stage = assessment_data.get('visual_analysis', {}).get('writing_stage', '').upper()
    
    # Only show this note if child demonstrates writing ability (CONVENTIONAL or EMERGING)
    # but drew for sun tasks - this shows metacognitive awareness that they don't know how to spell "sun"
    # Do NOT show for SCRIBBLES/LETTER_LIKE - those children aren't making a metacognitive choice
    child_can_write = writing_stage in ['CONVENTIONAL', 'EMERGING']
    
    if pair2_both_drawings and child_can_write:
        story.append(Spacer(1, 10))
        metacog_style = ParagraphStyle('MetacogStyle', parent=styles['Normal'], fontSize=10, textColor=HexColor('#5D4E37'), leading=14)
        
        metacog_text = (
            f"Note: Both sun samples were drawings, but this does not mean {child_name} lacks understanding. "
            f"Research shows that children write with more skill when they know how to spell a word (like their name) "
            f"than when they do not. When children do not yet know how to write a word (like 'sun'), drawing is a "
            f"natural response. This is age-appropriate at {age_display}."
        )
        
        metacog_content = [[Paragraph(metacog_text, metacog_style)]]
        metacog_table = Table(metacog_content, colWidths=[available_width - 20])
        metacog_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#FFF9E6')),
            ('BOX', (0, 0), (-1, -1), 1, warning_amber),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(metacog_table)
        story.append(Spacer(1, 15))
    
    story.append(Spacer(1, 15))
    
    # What child showed us
    story.append(Paragraph(f"What {child_name} Showed Us", heading_style))
           
    strengths = build_pdf_strengths(visual_analysis, child_name, stage, child_age_months)
    strength_style = ParagraphStyle('StrengthItem', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=4)
    for strength in strengths:
        story.append(Paragraph(f"✓ {strength}", strength_style))
    
    # Parent observations
    verbal = assessment_data.get('verbal_behaviour', {})
    interpretations = verbal.get('interpretations', [])
    if interpretations:
        story.append(Spacer(1, 15))
        story.append(Paragraph("Your Observations", heading_style))
        obs_bullet_style = ParagraphStyle('ObsBullet', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=4)
        for interp in interpretations:
            story.append(Paragraph(f"• {interp}", obs_bullet_style))
    
    # Activities
    story.append(Spacer(1, 15))
# SAFETY DISCLAIMER
    disclaimer_style = ParagraphStyle(
        'DisclaimerStyle',
        parent=styles['Normal'],
        fontSize=9,
        textColor=HexColor('#4a4a4a'),
        leading=11,
        spaceAfter=6,
        leftIndent=10,
        rightIndent=10
    )
    
    disclaimer_heading_style = ParagraphStyle(
        'DisclaimerHeading',
        parent=styles['Normal'],
        fontSize=10,
        textColor=HexColor('#2c2c2c'),
        fontName='Helvetica-Bold',
        spaceAfter=4,
        leftIndent=10,
        rightIndent=10
    )
    
    # Create disclaimer box content
    disclaimer_heading = Paragraph("IMPORTANT SAFETY INFORMATION", disclaimer_heading_style)
    disclaimer_text = Paragraph(
        "All activities recommended in this report require active adult supervision. These activities are designed for children aged 24-42 months based on typical development, but every child is unique. Parents and caregivers must use their own judgment to determine whether each activity is appropriate for their child's individual abilities, interests and safety needs.<br/><br/>"
        "This report provides educational guidance only and is not a substitute for medical, therapeutic or professional advice. More Handwriting is not liable for any injury, accident or adverse outcome resulting from activities undertaken based on this report. Parents and caregivers assume full responsibility for their child's safety during all activities.",
        disclaimer_style
    )
    
    # Create table for grey box effect
    disclaimer_table = Table(
        [[disclaimer_heading], [disclaimer_text]],
        colWidths=[480]
    )
    disclaimer_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), HexColor('#f5f5f5')),
        ('BOX', (0, 0), (-1, -1), 1, HexColor('#d0d0d0')),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    story.append(KeepTogether([disclaimer_table, Spacer(1, 15)]))
    # END DISCLAIMER
    
    story.append(Paragraph("Recommended Activities", heading_style))
    
    # Get email for sibling detection
    recipient_email = assessment_data.get('email', '')
    activities = get_recommended_activities(stage, total_score, child_name, child_age_months, recipient_email)
 
   
    for activity in activities:
        story.append(Paragraph(activity['title'], activity_title_style))
        story.append(Paragraph(activity['description'], activity_body_style))
        if activity.get('research_note'):
            research_note_style = ParagraphStyle('ResearchNote', fontSize=8, textColor=medium_grey, leading=11, spaceAfter=8, leftIndent=10)
            story.append(Paragraph(f"<i>{activity['research_note']}</i>", research_note_style))
    
    # =========================================================================
    # NEW SECTION: What to Look For Next
    # =========================================================================
    story.append(Spacer(1, 20))
    story.append(Paragraph("What to Look For Next", heading_style))
    
    milestones = get_developmental_milestones(stage, child_age_months, child_name)
    milestone_style = ParagraphStyle('MilestoneItem', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=6, leftIndent=15)
    milestone_intro = ParagraphStyle('MilestoneIntro', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=8)
    
    story.append(Paragraph(milestones['intro'], milestone_intro))
    for milestone in milestones['signs']:
        story.append(Paragraph(f"• {milestone}", milestone_style))
    
    if milestones.get('note'):
        note_style = ParagraphStyle('MilestoneNote', fontSize=9, textColor=medium_grey, leading=13, spaceAfter=6, spaceBefore=8)
        story.append(Paragraph(f"<i>{milestones['note']}</i>", note_style))
    
    # =========================================================================
    # NEW SECTION: Understanding This Assessment
    # =========================================================================
    story.append(Spacer(1, 20))
    story.append(Paragraph("Understanding This Assessment", heading_style))
    
    understanding_style = ParagraphStyle('UnderstandingText', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=8)
    
    story.append(Paragraph(
        "This assessment is based on peer-reviewed research into how young children develop an understanding that writing and drawing are different. "
        "Researchers found that children as young as 2 years and 8 months begin to show this understanding in subtle but measurable ways.",
        understanding_style
    ))
    
    story.append(Paragraph(
        "When children understand the difference, their writing tends to be: smaller than their drawings, darker in colour, "
        "more angular (with straighter lines) and sparser (with fewer marks). Their drawings tend to be: larger, more colourful, "
        "more curved and denser. This assessment measures these differences across four samples.",
        understanding_style
    ))
    
    story.append(Paragraph(
        f"<b>What the stages mean:</b>", understanding_style
    ))
    
    stages_explanation = [
        "<b>Early Days:</b> Writing and drawing look similar. The child is still exploring mark-making, which is a normal and necessary stage before differentiation develops.",
        "<b>Beginning Explorer:</b> Some differences are emerging between writing and drawing. The child is starting to understand these are different activities.",
        "<b>Strong Start:</b> Clear differences between writing and drawing. The child shows good understanding that these serve different purposes."
    ]
    
    for stage_exp in stages_explanation:
        story.append(Paragraph(f"• {stage_exp}", milestone_style))
    
    # =========================================================================
    # NEW SECTION: Try Again in 3 Months
    # =========================================================================
    story.append(Spacer(1, 20))
    
    # Create a highlighted box for the reassessment prompt
    # Skip for conventional writers - they do not need to reassess
    if stage != "ALREADY WRITING":
        reassess_elements = []
        
        reassess_title = ParagraphStyle('ReassessTitle', fontSize=11, textColor=primary_blue, alignment=TA_LEFT, fontName='Helvetica-Bold', spaceAfter=6)
        reassess_body = ParagraphStyle('ReassessBody', fontSize=9, textColor=dark_grey, leading=13)
        
        reassess_elements.append([Paragraph("Track Progress: Try Again in 3 Months", reassess_title)])
        
        if stage == "STRONG START":
            reassess_text = (
                f"At {child_name}'s current stage, you might like to repeat this activity in 3-6 months to see how their "
                f"writing and drawing continue to develop. Look for increasingly letter-like shapes in their writing attempts, "
                f"and more detailed, representational drawings."
            )
        elif stage == "BEGINNING EXPLORER":
            reassess_text = (
                f"Children at this stage often make rapid progress. Try this activity again in 3 months to see how {child_name}'s "
                f"understanding has developed. With the activities suggested above, you may see clearer differences between "
                f"their writing and drawing next time."
            )
        else:
            reassess_text = (
                f"Repeating this activity in 3 months will show you how {child_name}'s mark-making is developing. "
                f"With regular exposure to books and print and the activities suggested above, you may see the first signs "
                f"of differentiation emerging. Remember that children develop at different rates and all progress is valuable."
            )
        
        reassess_elements.append([Paragraph(reassess_text, reassess_body)])
        reassess_elements.append([Paragraph(f"<b>Suggested reassessment date:</b> {(datetime.now() + timedelta(days=90)).strftime('%B %Y')}", reassess_body)])
        
        reassess_box = Table(reassess_elements, colWidths=[available_width - 30])
        reassess_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), highlight_bg),
            ('BOX', (0, 0), (-1, -1), 1, warning_amber),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 12),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ]))
        story.append(reassess_box)
    else:
        # For conventional writers, show celebratory message instead
        congrats_elements = []
        
        congrats_title = ParagraphStyle('CongratsTitle', fontSize=11, textColor=success_green, alignment=TA_LEFT, fontName='Helvetica-Bold', spaceAfter=6)
        congrats_body = ParagraphStyle('CongratsBody', fontSize=9, textColor=dark_grey, leading=13)
        
        congrats_elements.append([Paragraph("Already Writing!", congrats_title)])
        congrats_text = (
            f"{child_name} is already writing real letters - that is wonderful! This particular assessment is designed for "
            f"children who are still learning that writing and drawing are different, so the scoring system is not really "
            f"relevant for where {child_name} is now. Instead, enjoy building on their strong foundation with the "
            f"activities suggested above."
        )
        congrats_elements.append([Paragraph(congrats_text, congrats_body)])
        
        congrats_box = Table(congrats_elements, colWidths=[available_width - 30])
        congrats_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), light_green),
            ('BOX', (0, 0), (-1, -1), 1, success_green),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 12),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ]))
        story.append(congrats_box)
    
    # Detailed Breakdown section - skip for Already Writing (scores not meaningful)
    if stage != "ALREADY WRITING":
        story.append(Spacer(1, 25))
        story.append(Paragraph("—  —  —  —  —", ParagraphStyle('Divider', fontSize=10, textColor=light_grey, alignment=TA_CENTER, spaceAfter=15)))
        story.append(Spacer(1, 10))
        
        breakdown_elements = []
        
        breakdown_title = ParagraphStyle('BreakdownTitle', fontSize=12, textColor=primary_blue, alignment=TA_CENTER, fontName='Helvetica-Bold', spaceAfter=4)
        breakdown_elements.append([Paragraph("Detailed Breakdown", breakdown_title)])
        
        breakdown_sub = ParagraphStyle('BreakdownSub', fontSize=8, textColor=medium_grey, alignment=TA_CENTER, spaceAfter=12)
    
        breakdown_elements.append([Paragraph("For parents who would like more detail. These scores help us understand where your child is - not a test or judgement.", breakdown_sub)])
        
        if stage == "STRONG START":
            score_context = "Clear differentiation between writing and drawing."
        elif stage == "BEGINNING EXPLORER":
            score_context = "Emerging awareness that writing and drawing are different."
        else:
            score_context = "Still exploring mark-making - a normal starting point."
        
        score_style = ParagraphStyle('ScoreMain', fontSize=11, textColor=dark_grey, alignment=TA_CENTER, fontName='Helvetica-Bold', spaceBefore=8)
        breakdown_elements.append([Paragraph(f"Overall: {total_score} out of {max_score} points ({percentage:.0f}%)", score_style)])
        
        context_style = ParagraphStyle('ScoreContext', fontSize=9, textColor=medium_grey, alignment=TA_CENTER, spaceAfter=15)
        breakdown_elements.append([Paragraph(score_context, context_style)])
        
        score_rows = []
        label_style = ParagraphStyle('ScoreLabel', fontSize=9, textColor=dark_grey, fontName='Helvetica-Bold')
        value_style = ParagraphStyle('ScoreValue', fontSize=8, textColor=medium_grey)
        
        if pairs_completed.get('pair1'):
            p1 = visual_analysis.get('pair1_scores', {})
            size = p1.get('size_difference', {}).get('score', 0)
            colour = p1.get('colour_differentiation', {}).get('score', 0)
            marks = p1.get('angularity', {}).get('score', 0)
            density = p1.get('density', {}).get('score', 0)
            shape = p1.get('shape_features', {}).get('score', 0)
            score_rows.append([
                Paragraph("Name and Self Portrait", label_style),
                Paragraph(f"Size {size}/3", value_style),
                Paragraph(f"Colour {colour}/2", value_style),
                Paragraph(f"Marks {marks}/2", value_style),
                Paragraph(f"Shape {shape}/1", value_style)
            ])
        
        if pairs_completed.get('pair2'):
            p2 = visual_analysis.get('pair2_scores', {})
            size = p2.get('size_difference', {}).get('score', 0)
            colour = p2.get('colour_object_appropriate', {}).get('score', 0)
            marks = p2.get('angularity', {}).get('score', 0)
            shape = p2.get('shape_features', {}).get('score', 0)
            score_rows.append([
                Paragraph("Sun Writing and Drawing", label_style),
                Paragraph(f"Size {size}/3", value_style),
                Paragraph(f"Colour {colour}/3", value_style),
                Paragraph(f"Marks {marks}/2", value_style),
                Paragraph(f"Shape {shape}/3", value_style)
            ])
        
        if pairs_completed.get('pair1') and pairs_completed.get('pair2'):
            cp = visual_analysis.get('cross_pair_scores', {})
            if cp:
                writing = cp.get('writing_consistency', {}).get('score', 0)
                drawing = cp.get('drawing_variety', {}).get('score', 0)
                score_rows.append([
                    Paragraph("Consistency", label_style),
                    Paragraph(f"Writing {writing}/2", value_style),
                    Paragraph(f"Drawing {drawing}/1", value_style),
                    Paragraph("", value_style),
                    Paragraph("", value_style)
                ])
        
        if score_rows:
            scores_table = Table(score_rows, colWidths=[120, 70, 70, 70, 70])
            scores_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LINEABOVE', (0, 0), (-1, 0), 0.5, HexColor('#DDDDDD')),
                ('LINEBELOW', (0, -1), (-1, -1), 0.5, HexColor('#DDDDDD')),
            ]))
            breakdown_elements.append([scores_table])
    
        breakdown_box = Table(breakdown_elements, colWidths=[available_width - 40])
        breakdown_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#FAFAFA')),
            ('BOX', (0, 0), (-1, -1), 1, HexColor('#E0E0E0')),
            ('TOPPADDING', (0, 0), (-1, 0), 15),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 15),
            ('LEFTPADDING', (0, 0), (-1, -1), 20),
            ('RIGHTPADDING', (0, 0), (-1, -1), 20),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        story.append(breakdown_box)
    
    # Footer
    story.append(Spacer(1, 30))
    
    footer_style = ParagraphStyle('Footer', fontSize=8, textColor=medium_grey, alignment=TA_CENTER, leading=12)
    story.append(Paragraph("This report is informed by peer-reviewed research into how young children develop", footer_style))
    story.append(Paragraph("an understanding that writing and drawing are different.", footer_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Questions? contact@morehandwriting.co.uk  •  morehandwriting.co.uk", footer_style))
    
    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


def build_pdf_strengths(visual_analysis: dict, child_name: str, stage: str, age_months: int) -> list:
    """Build list of strengths for PDF based on visual analysis scores and developmental context."""
    
    # For conventional writers, return different strengths
    if stage == "ALREADY WRITING":
        return [
            f"{child_name} is writing real, recognisable letters - this is a wonderful achievement!",
            f"{child_name} clearly understands that writing and drawing are different activities.",
            f"{child_name} has moved beyond the early mark-making stage into real letter writing.",
            f"{child_name}'s writing shows they are ready for activities that build on this strong foundation."
        ]
    
    strengths = []
    observations = visual_analysis.get('observations', {})
    
    obs1 = str(observations.get('name_writing', '')).lower()
    obs2 = str(observations.get('self_portrait', '')).lower()
    obs3 = str(observations.get('sun_writing', '')).lower()
    obs4 = str(observations.get('sun_drawing', '')).lower()
    
    pair1_identical = 'identical' in obs1 or 'identical' in obs2 or 'same as' in obs1 or 'same as' in obs2
    pair2_identical = 'identical' in obs3 or 'identical' in obs4 or 'same as' in obs3 or 'same as' in obs4
    
    # Add strengths based on scores
    if visual_analysis.get('pair1_scores') and not pair1_identical:
        p1 = visual_analysis['pair1_scores']
        if p1.get('size_difference', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} made their name writing smaller than their self-portrait, showing an understanding that writing typically takes up less space than pictures.")
        if p1.get('colour_differentiation', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} chose darker colours for writing their name, showing awareness that writing and drawing are different activities.")
        if p1.get('angularity', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} used more angular marks for writing and curved marks for drawing, reflecting how real writing and pictures look different.")
        if p1.get('shape_features', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} included letter-like shapes in their name writing attempt.")
    
    if visual_analysis.get('pair2_scores') and not pair2_identical:
        p2 = visual_analysis['pair2_scores']
        if p2.get('size_difference', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} wrote 'sun' smaller than they drew the sun, showing awareness that words and pictures have different sizes.")
        if p2.get('colour_object_appropriate', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} used appropriate colours - darker for writing and brighter for the sun drawing.")
        if p2.get('shape_features', {}).get('score', 0) >= 1 and p2.get('size_difference', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} drew a recognisable sun shape while making their writing look distinctly different.")
    
    if visual_analysis.get('cross_pair_scores') and not pair1_identical and not pair2_identical:
        cp = visual_analysis['cross_pair_scores']
        if cp.get('writing_consistency', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} showed consistency in their approach to writing across different tasks.")
        if cp.get('drawing_variety', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} created appropriately different drawings for different subjects.")
    
    strongest = str(visual_analysis.get('strongest_evidence', '')).lower()
    if strongest and 'identical' not in strongest and 'same image' not in strongest and 'same as' not in strongest:
        strengths.append(visual_analysis.get('strongest_evidence'))
    
    # For Early Days, provide age-contextualised encouragement
    if len(strengths) == 0:
        if age_months < 32:
            strengths.append(f"{child_name} is engaging with mark-making activities, which is the essential first step before children begin to differentiate between writing and drawing.")
            strengths.append(f"At this age, {child_name}'s focus on making marks - regardless of what they look like - is exactly what builds the foundation for later writing development.")
        else:
            strengths.append(f"{child_name} willingly engaged with both the writing and drawing tasks.")
            strengths.append(f"{child_name} is using consistent approaches to mark-making, which shows developing motor control.")
    
    return strengths[:4]


def colours_match(colour1: str, colour2: str) -> bool:
    """
    Check if two colour names are semantically the same.
    Handles colour families (e.g., 'navy' matches 'dark blue').
    """
    if not colour1 or not colour2:
        return False
    
    colour1 = colour1.lower().strip()
    colour2 = colour2.lower().strip()
    
    # Exact match
    if colour1 == colour2:
        return True
    
    # Word boundary partial match (avoid "red" matching "bored")
    import re
    # Check if one is a complete word within the other
    if re.search(rf'\b{re.escape(colour1)}\b', colour2) or re.search(rf'\b{re.escape(colour2)}\b', colour1):
        return True
    
    # Colour families (FIXED: removed duplications)
    colour_families = {
        'blue': ['navy', 'dark blue', 'light blue', 'royal blue', 'sky blue', 'turquoise', 'teal', 'cyan'],
        'red': ['dark red', 'crimson', 'scarlet', 'maroon', 'burgundy'],
        'yellow': ['gold', 'golden', 'lemon'],
        'green': ['dark green', 'light green', 'lime', 'olive', 'forest green'],
        'grey': ['gray', 'silver', 'charcoal'],  # charcoal only here
        'purple': ['violet', 'lavender', 'lilac', 'mauve', 'plum'],
        'pink': ['rose', 'fuchsia', 'magenta', 'hot pink'],
        'orange': ['coral', 'peach', 'tangerine'],
        'brown': ['tan', 'beige', 'chocolate', 'bronze'],
        'black': ['ebony'],  # removed charcoal from here
        'white': ['ivory', 'pearl', 'cream']  # cream moved here from yellow
    }
    
    # Check if both colours belong to the same family
    for base_colour, variations in colour_families.items():
        if ((colour1 == base_colour or colour1 in variations) and 
            (colour2 == base_colour or colour2 in variations)):
            return True
    
    return False




def generate_comprehensive_summary(visual_analysis: dict, child_name: str, stage: str, age_months: int, favourite_colour_stated: str = '') -> str:
    """Generate a detailed, research-informed summary paragraph for the PDF report.
    
    Links findings to the research benchmark of 2 years 8 months and provides
    age-appropriate context.
    """
    observations = visual_analysis.get('observations', {})
    
    age_years = age_months // 12
    age_remainder = age_months % 12
    age_string = f"{age_years} years"
    if age_remainder > 0:
        age_string += f" and {age_remainder} months"
    
    # Handle conventional writers first
    if stage == "ALREADY WRITING":
        writing_stage_reasoning = visual_analysis.get('writing_stage_reasoning', '')
        favourite_colour_detected = visual_analysis.get('favourite_colour_detected', 'none')  # ADD THIS LINE
        
        summary = f"Great news! At {age_string} old, {child_name} is already writing real, recognisable letters. "
        summary += "This assessment is designed for children who are still learning that writing and drawing are different - "
        summary += f"but {child_name} has already mastered this understanding and moved into real letter writing. "
        if writing_stage_reasoning:
            summary += f"Specifically, we observed: {writing_stage_reasoning} "
        
        # ADD THIS ENTIRE BLOCK:        
     
       # Smart favourite colour detection - with robust checks
        if favourite_colour_detected and favourite_colour_detected.lower() not in ['none', '', 'n/a', 'null']:
            if favourite_colour_stated and favourite_colour_stated.lower() not in ['', 'no_preference', 'none']:
                # Parent stated a preference
                if colours_match(favourite_colour_detected, favourite_colour_stated):
                    summary += f"You mentioned that {favourite_colour_detected} is {child_name}'s favourite colour, and we can see this reflected in the samples. "
                else:
                    summary += f"You mentioned {favourite_colour_stated} as {child_name}'s favourite colour. In these samples, we noticed {favourite_colour_detected} appearing frequently. "
            else:
                # No parent input, but AI detected a colour
                summary += f"We noticed {favourite_colour_detected} appearing frequently across the samples. "
                
        summary += f"The numerical score is not really meaningful for {child_name} at this stage - what matters is that they have a strong foundation to build on. The activities below are designed to extend their existing skills."
        return summary
        
    obs1 = str(observations.get('name_writing', '')).lower()
    obs2 = str(observations.get('self_portrait', '')).lower()
    obs3 = str(observations.get('sun_writing', '')).lower()
    obs4 = str(observations.get('sun_drawing', '')).lower()
    
    pair1_identical = 'identical' in obs1 or 'identical' in obs2 or 'same as' in obs1 or 'same as' in obs2
    pair2_identical = 'identical' in obs3 or 'identical' in obs4 or 'same as' in obs3 or 'same as' in obs4
    both_identical = pair1_identical and pair2_identical
    
    p1_scores = visual_analysis.get('pair1_scores', {})
    p2_scores = visual_analysis.get('pair2_scores', {})
    
    if stage == "STRONG START":
        summary = f"At {age_string} old, {child_name} is demonstrating a clear understanding that writing and drawing are different. "
        
        differences = []
        if p1_scores.get('size_difference', {}).get('score', 0) >= 2 or p2_scores.get('size_difference', {}).get('score', 0) >= 2:
            differences.append("making writing smaller than drawings")
        if p1_scores.get('colour_differentiation', {}).get('score', 0) >= 2 or p2_scores.get('colour_object_appropriate', {}).get('score', 0) >= 2:
            differences.append("choosing different colours for writing and drawing")
        if p1_scores.get('angularity', {}).get('score', 0) >= 2 or p2_scores.get('angularity', {}).get('score', 0) >= 2:
            differences.append("using different types of marks")
        
        if differences:
            summary += f"This is evident in {' and '.join(differences)}. "
        
        summary += "Research has found that children typically begin to make this distinction from around 2 years and 8 months. "
        summary += f"{child_name} is showing exactly the kind of emerging literacy awareness that supports later reading and writing development. "
        summary += "The activities below will help build on this strong foundation."
        
    elif stage == "BEGINNING EXPLORER":
        summary = f"At {age_string} old, {child_name} is beginning to show awareness that writing and drawing are different activities. "
        
        emerging = []
        if p1_scores.get('size_difference', {}).get('score', 0) >= 1 or p2_scores.get('size_difference', {}).get('score', 0) >= 1:
            emerging.append("some variation in the size of marks")
        if p1_scores.get('colour_differentiation', {}).get('score', 0) >= 1 or p2_scores.get('colour_object_appropriate', {}).get('score', 0) >= 1:
            emerging.append("different colour choices")
        if p1_scores.get('shape_features', {}).get('score', 0) >= 1 or p2_scores.get('shape_features', {}).get('score', 0) >= 1:
            emerging.append("early attempts at letter-like shapes")
        
        if emerging:
            summary += f"The samples show {' and '.join(emerging)}, which indicates this understanding is developing. "
        
        summary += "Research has found that children typically begin distinguishing writing from drawing from around 2 years and 8 months. "
        summary += f"{child_name} is on this developmental path and the activities suggested below will support continued progress."
        
    else:  # EARLY DAYS
        summary = f"At {age_string} old, {child_name}'s writing and drawing samples look quite similar. "
        
        if both_identical:
            summary += f"When asked to write and draw, {child_name} produced very similar marks for both tasks. "
        
        # Age-specific context based on the 2y8m benchmark
        if age_months < 32:
            # Under 2y8m
            summary += "Research has found that children typically begin to distinguish between writing and drawing from around 2 years and 8 months. "
            summary += f"At {child_name}'s current age, the focus should be on enjoying making marks and having positive experiences with crayons, pencils and paper. "
            summary += "This builds the foundation for later differentiation."
        elif age_months < 42:
            # 2y8m to 3y6m
            summary += "Research has found that children typically begin to distinguish between writing and drawing from around 2 years and 8 months, though this varies between children. "
            summary += f"{child_name} would benefit from activities that naturally highlight the differences between writing and drawing - such as pointing out words and pictures during story time. "
            summary += "The activities below are designed to support this emerging awareness."
        else:
            # 3y6m to 4y
            summary += "Research has found that most children begin to distinguish between writing and drawing from around 2 years and 8 months. "
            summary += f"With regular exposure to books, print and the activities suggested below, {child_name} will develop this understanding. "
            summary += "Every child develops at their own pace and with the right support, progress at this stage is often rapid."
               
    # Add favourite colour note for all non-conventional stages
    favourite_colour_detected = visual_analysis.get('favourite_colour_detected', 'none')
    if favourite_colour_detected and favourite_colour_detected.lower() not in ['none', '', 'n/a', 'null']:
        if favourite_colour_stated and favourite_colour_stated.lower() not in ['', 'no_preference', 'none']:
            if colours_match(favourite_colour_detected, favourite_colour_stated):
                summary += f" We noticed {child_name} used {favourite_colour_detected} frequently — their favourite colour!"
            else:
                summary += f" We noticed {favourite_colour_detected} appearing frequently across the samples."
        else:
            summary += f" We noticed {favourite_colour_detected} appearing frequently across the samples."
    
    return summary

# =============================================================================
# ACTIVITY POOL - 50+ Research-Based Activities
# =============================================================================

ACTIVITY_POOL = [
    # === MARK-MAKING FOUNDATIONS (Ages 24-30m, Early Days) ===
    {
        "title": "Enjoying Mark-Making Together",
        "description": "The most important thing right now is for {child_name} to enjoy making marks. Offer crayons, chalk, finger paint - whatever {child_name} likes. Sit alongside and make your own marks. Say 'I love your marks!' without asking what they are or trying to correct them.",
        "research_note": "Children who have positive early experiences with mark-making are more likely to engage with writing later.",
        "age_bands": ["24-30"],
        "stages": ["scribbles"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Big Movements, Big Marks",
        "description": "Give {child_name} very large paper (or tape several sheets together) and chunky crayons. Encourage big arm movements - circles, swooshes, up and down. This builds the shoulder and arm muscles needed for later writing control.",
        "research_note": None,
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },

{
        "title": "Outdoor Mark-Making Adventures",
        "description": "Let {child_name} make marks outdoors: chalk on paving stones, paintbrushes with water on fences, or smooth twigs in mud (supervise carefully with sticks to prevent eye injuries). Different surfaces and tools build control and confidence.",
        "research_note": "Varied mark-making experiences develop motor skills and creative confidence. Always supervise when using sticks or twigs.",
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Looking at Books Together",
        "description": "Share books with {child_name} every day, even just for a few minutes. Let {child_name} turn the pages and point at things. Occasionally point to the words as you read a line. This exposure to print builds familiarity that will support later learning.",
        "research_note": "Frequency of shared reading is one of the strongest predictors of later literacy.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Pointing Out Print Naturally",
        "description": "Throughout the day, casually point to words: 'This says OPEN', 'Look, this word tells us it is milk.' Keep it very brief and natural - just a sentence here and there. This helps {child_name} begin to notice that print is everywhere.",
        "research_note": None,
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["print_awareness"],
        "types": ["indoor", "outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Making Marks with Purpose",
        "description": "Give {child_name} reasons to make marks: 'Can you draw what you would like for dinner?' or 'Let us make marks on this card for Grandma.' Even if the marks do not look like anything recognisable, responding to them as meaningful ('Oh, you want pasta!') helps {child_name} understand that marks carry meaning.",
        "research_note": "Children who understand that marks carry meaning make faster progress in learning to write.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Circular Scribbles Practice",
        "description": "Encourage {child_name} to make big circular scribbles - going round and round on paper. This circular motion is crucial for later letter formation. Make it fun by pretending to stir a giant pot or draw the sun going round and round.",
        "research_note": "Circular scribbles are a key developmental milestone that precedes letter formation.",
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "You Draw, I Will Write",
        "description": "After {child_name} draws a picture, offer to write a word or sentence about it underneath. Make it visible by saying 'You drew a lovely house - I will write the word house here. See how my writing looks different from your picture?' This models the relationship between pictures and words.",
        "research_note": "Seeing adults write in response to drawings helps children understand that writing carries meaning.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },

{
        "title": "Lots of Different Mark-Making Tools",
        "description": "Offer varied tools: chunky crayons, chalk, paintbrushes, sticks in sand. Let {child_name} see you use the same tools. Children develop control through practice with different materials.",
        "research_note": "Fine motor development through varied mark-making supports later handwriting.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "outdoor", "limited_resources"]
    },
    {
        "title": "Writing and Drawing Side by Side",
        "description": "When {child_name} makes a picture, sit beside them and write a word related to their drawing on the same page. Point out how they look different: 'You drew a big sun, and I wrote a little word - sun.' This contrast helps children see the difference between pictures and writing.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === PRINT AWARENESS & DIFFERENTIATION (Ages 30-42m, Beginning Explorer/Strong Start) ===
    {
        "title": "Words and Pictures at Story Time",
        "description": "When you read together, occasionally run your finger under the words as you read a line. Point to a picture and say 'Look at this!' then point to the words and say 'And these are the words that tell us about it.' Keep it brief - the story is what matters most.",
        "research_note": "Research shows that simply drawing attention to print helps children begin to notice it.",
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["print_awareness", "differentiation"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Noticing Words and Pictures",
        "description": "During story time, occasionally pause to point out the difference between words and pictures. You might say 'Look, here is a picture of a dog, and this word here says dog.' After a page, you could ask {child_name}: 'Can you point to some words? Can you point to a picture?' Keep it playful.",
        "research_note": "Research shows that this kind of 'print referencing' during shared reading significantly boosts print awareness.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["print_awareness", "differentiation"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Environmental Print Hunt",
        "description": "When you are out, point out print naturally: 'Look, that sign says STOP' or 'This packet says biscuits.' Ask {child_name} to spot letters from their name on signs or packaging. Children who notice print in their environment develop stronger literacy foundations.",
        "research_note": "Children who can recognise environmental print show stronger early reading skills.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging", "conventional"],
        "skills": ["print_awareness", "letter_knowledge"],
        "types": ["outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Pretend Writing in Play",
        "description": "Set up a pretend post office, café or shop with paper, envelopes and pencils. Let {child_name} 'write' orders, letters or receipts as part of play. This purposeful mark-making is more valuable than practice sheets.",
        "research_note": "Research shows that children who engage in play-based writing develop better understanding of print functions.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["purposeful_writing", "play"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "Thinking Aloud When You Write",
        "description": "When you write a shopping list, card or note, let {child_name} watch. Think aloud: 'I need to remember to buy milk, so I am writing M-I-L-K.' Children learn from seeing adults use print for real purposes.",
        "research_note": "Children whose parents model everyday writing show greater understanding of print's purpose.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["print_awareness", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === NAME WRITING FOCUS (Ages 30-42m, All Stages) ===
    {
        "title": "Writing {child_name}'s Name",
        "description": "Help {child_name} practise writing their name using chunky crayons and big paper. Point out letters: 'Your name starts with this letter - can you spot it anywhere else today?' Celebrate all attempts - the process matters more than perfection.",
        "research_note": "Name writing is one of the strongest predictors of later literacy success.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Name Recognition Games",
        "description": "Write {child_name}'s name on cards or sticky notes and hide them around the house. Ask {child_name} to find them. Talk about the letters: 'Your name starts with... Can you find that letter?' This makes letter learning playful.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "Tracing Over Your Writing",
        "description": "Write {child_name}'s name in thick crayon, then let them trace over it with a different colour. This helps them feel the movements needed for each letter. Gradually fade support: thick tracing → thin lines → dots → independent.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "fine_motor"],
        "types": ["indoor", "limited_resources", "with_help"]
    },

# === LETTER KNOWLEDGE & PHONICS (Ages 30-42m, Emerging/Conventional) ===
    {
        "title": "Letter Sounds in Daily Life",
        "description": "Point out letter sounds naturally: 'Ball starts with buh - B!' or 'Your name starts with... what sound?' Keep it casual and follow {child_name}'s interest. A few seconds here and there builds awareness without pressure.",
        "research_note": "Children who learn letter sounds alongside letter names show stronger reading development.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["phonics", "letter_knowledge"],
        "types": ["indoor", "outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Alphabet Books and Songs",
        "description": "Share alphabet books with {child_name}, singing the ABC song together. Point to letters as you sing. Choose books with clear, simple pictures for each letter. This makes letter learning multi-sensory and fun.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "phonics"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Making Letters with Play-Dough",
        "description": "Roll play-dough into 'snakes' and help {child_name} shape them into letters, starting with letters from their name. Talk about the shapes: 'We make a line down, then a curve' for P. This tactile approach helps letter formation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "fine_motor"],
        "types": ["indoor", "play"]
    },
    {
        "title": "Letter Hunt in Books",
        "description": "Pick a letter (start with the first letter of {child_name}'s name) and hunt for it together in a favourite book. Count how many you find. This builds letter recognition in a natural context.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help", "play"]
    },
  {
        "title": "Magnetic Letters on the Fridge",
        "description": "Keep large magnetic letters (at least 2 inches / 5cm tall) on the fridge at {child_name}'s height. Show them the letters in their name and let them arrange them. Talk about the letters casually while cooking: 'Can you find the M? That is for Mummy/Mum.' Always supervise to ensure letters stay on the fridge and are not mouthed.",
        "research_note": "Use only magnetic letters that are at least 2 inches (5cm) tall to reduce choking risk. Not suitable for children who still mouth objects frequently. Adult supervision required.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "name_writing"],
        "types": ["indoor", "with_help", "play"]
    },
    
    # === FINE MOTOR & HAND STRENGTH (All Ages) ===
    {
        "title": "Threading and Lacing with Jumbo Beads",
        "description": "Threading JUMBO beads (at least 1.5 inches / 4cm in size) or large lacing cards builds the finger strength and control needed for writing. Use beads specifically designed for toddlers with thick laces. Let {child_name} work at their own pace - the process builds skills even if they do not complete the pattern. ALWAYS supervise closely.",
        "research_note": "Use only JUMBO beads (1.5 inches or larger) designed for toddlers. Fine motor activities like threading significantly improve pencil control. Adult supervision required at all times to prevent choking hazards.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["fine_motor"],
        "types": ["indoor", "with_help"]
    },
    {
        "title": "Tearing and Crumpling Paper",
        "description": "Let {child_name} tear newspaper into strips or crumple paper into balls. These simple activities build hand strength. Make it purposeful: 'Let us make confetti!' or 'Can you scrunch this really tight?'",
        "research_note": None,
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor"],
        "types": ["indoor", "limited_resources", "independent"]
    },
    {
        "title": "Playdough Squeezing and Rolling",
        "description": "Playing with playdough - squeezing, rolling, pinching - builds hand muscles essential for writing. No need for specific shapes; free exploration is valuable. Five minutes of playdough play strengthens hands for holding pencils.",
        "research_note": "Hand strengthening activities directly improve writing stamina and control.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["fine_motor"],
        "types": ["indoor", "play"]
    },
    {
        "title": "Using Tweezers and Tongs",
        "description": "Let {child_name} use child-safe tweezers or kitchen tongs to pick up pom-poms, cotton balls or small toys. This pincer grip is exactly what they need for holding a pencil. Make it a game: 'Can you move all the balls to this bowl?'",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["fine_motor"],
        "types": ["indoor", "play"]
    },
    {
        "title": "Painting at an Easel",
        "description": "Painting on a vertical surface (easel or paper taped to a wall) builds shoulder stability and wrist strength - both crucial for writing. Large brushstrokes develop arm control that later translates to pencil control.",
        "research_note": "Vertical surface work strengthens the shoulder muscles needed for stable handwriting.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "outdoor"]
    },
    
    # === CONVENTIONAL WRITERS (Ages 36-42m) ===
    {
        "title": "Building on Strong Foundations",
        "description": "Wonderful news - {child_name} is already writing real letters! This assessment is designed for children still learning that writing and drawing are different, so the scoring is not really relevant for {child_name}. Instead, focus on activities that build on their existing skills.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["name_writing", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
 
    {
        "title": "Simple Words and Labels",
        "description": "Encourage {child_name} to write simple words - labels for their drawings, names of family members, or words from their favourite books. Keep it playful and pressure-free.",
        "research_note": "Children who write for real purposes develop stronger motivation for literacy.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Reading Together for Comprehension",
        "description": "Since {child_name} already recognises that print carries meaning, story time can focus on enjoying stories together. Ask questions like 'What do you think will happen next?' or 'Why did the character do that?' to build comprehension skills.",
        "research_note": "Reading comprehension develops best through discussion and engagement with stories.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    # === CONVENTIONAL WRITERS - EXTENDED ACTIVITIES ===
    {
        "title": "Writing Simple Words",
        "description": "Encourage {child_name} to write simple 3-letter words like 'cat', 'dog', 'sun', 'mum', 'dad'. Sound out the letters together: 'c-a-t'. Celebrate every attempt - spelling will develop naturally with practice.",
        "research_note": "Children who attempt to write words, even with invented spellings, develop stronger phonemic awareness.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "phonics", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Writing Messages to Family",
        "description": "Help {child_name} write short messages or cards to family members - 'I love you', 'Happy Birthday', or just names. Real purposes for writing build motivation. Accept invented spellings warmly.",
        "research_note": "Writing for authentic purposes significantly increases children's motivation to write.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "name_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Labelling Drawings with Words",
        "description": "After {child_name} draws a picture, encourage them to write a word or two to label it - the name of what they drew, or a simple describing word. This connects drawing and writing purposefully.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Creating a Name Collection",
        "description": "Help {child_name} create a collection of names they can write - their name, family members, pets, friends. Make a special 'Names I Can Write' book or poster to display proudly.",
        "research_note": "Name writing extends naturally to writing other meaningful names in the child's life.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["name_writing", "letter_knowledge", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Writing a Shopping List Together",
        "description": "Before shopping, make a list together. Let {child_name} write words they know (milk, eggs, bread) while you help with harder ones. Use the list at the shop - this shows writing has real purpose.",
        "research_note": "Functional writing tasks build understanding that writing serves practical purposes.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Keeping a Simple Journal",
        "description": "Give {child_name} a special notebook for drawing and writing about their day. They might draw a picture and write one word or a short sentence underneath. Even 'I went park' is wonderful progress.",
        "research_note": "Regular, low-pressure writing practice builds confidence and fluency.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "fine_motor"],
        "types": ["indoor", "limited_resources", "independent"]
    },
    {
        "title": "Making Labels for Their Room",
        "description": "Help {child_name} make labels for things in their room - 'bed', 'toys', 'books', 'door'. They write the words on card and you help attach them. This creates a print-rich environment they created themselves.",
        "research_note": "Child-created environmental print reinforces the connection between spoken and written words.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Writing Thank You Notes",
        "description": "After receiving a gift or kindness, help {child_name} write a simple thank you note. They write what they can (their name, 'thank you', the person's name) and you help with the rest.",
        "research_note": "Thank you notes teach both social skills and purposeful writing simultaneously.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "name_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    
# === CREATIVE & PLAYFUL APPROACHES (All Ages, All Stages) ===
 {
        "title": "Writing on a Chalkboard or Whiteboard",
        "description": "Give {child_name} a small chalkboard with chunky chalk, or a whiteboard with washable markers. Let them make marks, lines, circles and letter shapes. This erasable surface is forgiving - mistakes disappear with a wipe! Much safer than sand for this age group.",
        "research_note": "Vertical surfaces like chalkboards and whiteboards build shoulder stability essential for handwriting control.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "fine_motor", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Rainbow Writing",
        "description": "Write {child_name}'s name in light pencil or crayon. Let them trace over it multiple times with different colours, creating a rainbow effect. This repetition builds muscle memory for letter formation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "fine_motor"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Writing Letters in the Air",
        "description": "Stand together and 'write' big letters in the air with your whole arm. Say the letter name as you form it. This builds spatial awareness and letter memory without the pressure of paper.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "fine_motor"],
        "types": ["indoor", "outdoor", "limited_resources", "play"]
    },
    {
        "title": "Making a Mark-Making Station",
        "description": "Set up a small table or corner with paper, crayons, pencils and markers always available. When {child_name} is interested, they can choose to make marks independently. Low-pressure availability encourages exploration.",
        "research_note": None,
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "independent"]
    },
    {
        "title": "Drawing What You See",
        "description": "When out on walks, sit together and let {child_name} draw things they can see - a tree, a car, a house. This purposeful drawing builds observation skills and hand control. No pressure for accuracy - the process matters.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["outdoor", "limited_resources"]
    },
    {
        "title": "Making Birthday Cards and Notes",
        "description": "When someone has a birthday or you want to say thank you, help {child_name} make a card. They can draw the picture while you write the words, or they can add their name. This shows writing serving a real purpose.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging", "conventional"],
        "skills": ["purposeful_writing", "mark_making"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === BUILDING DIFFERENTIATION AWARENESS (Ages 30-42m) ===
    {
        "title": "Comparing Writing and Drawing",
        "description": "After {child_name} makes marks, occasionally compare them: 'When you draw, you make big colourful pictures. When you write, you make small dark marks in a line.' Keep it observational, not corrective.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Sorting Pictures and Words",
        "description": "Cut out pictures and words from magazines or newspapers. Help {child_name} sort them into two piles: 'pictures' and 'words'. Talk about how they look different. This concrete sorting builds differentiation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "Two Pages: One for Drawing, One for Writing",
        "description": "Give {child_name} two sheets of paper side by side. Say 'This one is for drawing a big picture, and this one is for writing small marks.' Let them choose their own approach - the physical separation helps clarify the difference.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Noticing Writing is Smaller",
        "description": "When reading books, occasionally point out: 'Look how tiny these words are! And look how big this picture is!' This repeated observation helps {child_name} notice that writing and pictures typically differ in size.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === MOTOR CONTROL & PRE-WRITING PATTERNS (Ages 24-36m) ===
    {
        "title": "Dot-to-Dot Lines",
        "description": "Make simple dot patterns for {child_name} to connect: two dots to make a line, three dots to make a triangle, dots in a row. This builds the control needed for forming letters.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Making Zigzags and Waves",
        "description": "Show {child_name} how to make zigzag lines and wavy lines across the page. These pre-writing patterns build the hand control needed for letters. Make it fun: 'Draw the mountain path!' or 'Make waves in the sea!'",
        "research_note": None,
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Copying Simple Shapes",
        "description": "Draw simple shapes - circle, line, cross - and let {child_name} try to copy them nearby. These basic shapes are the building blocks of letters. Praise effort, not perfection.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    
    # === EMERGENT LITERACY ACTIVITIES (Ages 36-42m, Emerging/Conventional) ===
    {
        "title": "Writing Shopping Lists Together",
        "description": "Before shopping, write a list together. You write the words while {child_name} adds their marks or attempts at letters beside each item. This shows writing serving a practical purpose.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["emerging", "conventional"],
        "skills": ["purposeful_writing", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Labelling Toy Boxes",
        "description": "Help {child_name} make labels for their toy boxes. They can draw a picture of what goes inside, and you (or they) can add the word. This practical writing makes organisation meaningful.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["emerging", "conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Family Name Writing",
        "description": "Help {child_name} learn to write the names of family members, starting with short names. Make name cards they can trace or copy. Celebrating their attempts to write 'Mum', 'Dad', or siblings' names builds motivation.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["emerging", "conventional"],
        "skills": ["name_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
# === ENGAGING WITH BOOKS & STORIES (All Ages) ===
{
        "title": "Making Your Own Books",
        "description": "Fold a few sheets of paper in half to make a simple book. Secure the pages with tape or paperclips, or leave them loose (avoid staples which can injure small fingers). Let {child_name} draw pictures on each page while you write a word or sentence about each picture. Read the book together. This shows writing and pictures working together.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging", "conventional"],
        "skills": ["purposeful_writing", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Retelling Stories with Drawings",
        "description": "After reading a favourite story, give {child_name} paper to draw what happened. Ask them to tell you about their drawing. This connects stories with mark-making and builds narrative skills.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "purposeful_writing"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Letter of the Week",
        "description": "Choose one letter to focus on for a week - perhaps from {child_name}'s name. Point it out in books, on signs, on food packaging. Make that letter shape together. This repeated, relaxed exposure builds letter knowledge.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "print_awareness"],
        "types": ["indoor", "outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Texture Writing",
        "description": "Place paper over different textures (tree bark, fabric, coins under paper) and let {child_name} rub a crayon over it. Talk about how different surfaces create different marks. This builds awareness of mark-making possibilities.",
        "research_note": None,
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["indoor", "outdoor", "limited_resources"]
    },
    {
        "title": "Giant Outdoor Chalk Letters",
        "description": "Using chunky pavement chalk, draw giant letters on the ground outside. Let {child_name} walk along the letter shapes with their feet. This whole-body approach helps letter recognition and formation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "fine_motor"],
        "types": ["outdoor", "limited_resources", "play"]
    },
]

def get_recommended_activities(stage: str, score: int, child_name: str = "your child", age_months: int = 36, email: str = "") -> list:
    """Return 4-5 activities from ACTIVITY_POOL using prioritized scoring and randomization."""
    import random
    
    # Determine age band
    if age_months <= 30:
        age_band = "24-30"
    elif age_months <= 36:
        age_band = "30-36"
    else:
        age_band = "36-42"
    
    # Map stage to stage tags
    stage_clean = stage.upper().replace(" ", "_")
    if stage_clean == "ALREADY_WRITING":
        stage_tags = ["conventional"]
        is_conventional = True
    elif stage_clean == "STRONG_START":
        stage_tags = ["emerging"]
        is_conventional = False
    elif stage_clean == "BEGINNING_EXPLORER":
        stage_tags = ["letter_like", "emerging"]
        is_conventional = False
    else:
        stage_tags = ["scribbles", "letter_like"]
        is_conventional = False
    
    # Determine priority skills based on stage
    if stage_clean == "ALREADY_WRITING":
        priority_skills = ["purposeful_writing", "name_writing", "letter_knowledge", "phonics"]
    elif stage_clean == "STRONG_START":
        priority_skills = ["differentiation", "letter_knowledge", "name_writing", "print_awareness"]
    elif stage_clean == "BEGINNING_EXPLORER":
        priority_skills = ["differentiation", "purposeful_writing", "print_awareness", "letter_knowledge"]
    else:
        priority_skills = ["mark_making", "fine_motor", "print_awareness"]
    
    # Score each activity
    scored_activities = []
    for activity in ACTIVITY_POOL:
        score_total = 0
        
        # Age match (0-3 points)
        if age_band in activity["age_bands"]:
            score_total += 3
        else:
            adjacent_match = False
            if age_band == "24-30" and "30-36" in activity["age_bands"]:
                adjacent_match = True
            elif age_band == "30-36" and ("24-30" in activity["age_bands"] or "36-42" in activity["age_bands"]):
                adjacent_match = True
            elif age_band == "36-42" and "30-36" in activity["age_bands"]:
                adjacent_match = True
            
            if adjacent_match:
                score_total += 2
            elif len(activity["age_bands"]) >= 2:
                score_total += 1
        
        # Stage match - HEAVILY weight conventional for conventional writers
        stage_overlap = set(stage_tags) & set(activity["stages"])
        
        if is_conventional:
            if "conventional" in activity["stages"]:
                score_total += 5  # Big bonus for conventional-specific
            elif stage_overlap:
                score_total += 2
        else:
            if stage_overlap:
                if len(stage_overlap) >= 2:
                    score_total += 4
                else:
                    score_total += 3
            elif len(activity["stages"]) >= 3:
                score_total += 1
        
        # Skill relevance (0-4 points)
        skill_overlap = set(priority_skills) & set(activity["skills"])
        skill_points = min(len(skill_overlap), 4)
        score_total += skill_points
        
        # Bonus for purposeful_writing for conventional writers
        if is_conventional and "purposeful_writing" in activity["skills"]:
            score_total += 2
        
        scored_activities.append({
            "activity": activity,
            "score": score_total
        })
    
    # Sort by score descending
    scored_activities.sort(key=lambda x: x["score"], reverse=True)
    
    # For conventional writers, ensure we get activities tagged "conventional"
    if is_conventional:
        conventional_activities = [a for a in scored_activities if "conventional" in a["activity"]["stages"]]
        other_activities = [a for a in scored_activities if "conventional" not in a["activity"]["stages"]]
        
        random.shuffle(conventional_activities)
        random.shuffle(other_activities[:10])
        
        num_to_select = 5
        selected = conventional_activities[:num_to_select]
        
        if len(selected) < num_to_select:
            remaining = num_to_select - len(selected)
            selected.extend(other_activities[:remaining])
    else:
        pool_size = min(20, len(scored_activities))
        top_pool = scored_activities[:pool_size]
        random.shuffle(top_pool)
        
        num_to_select = 4
        selected = top_pool[:num_to_select]
    
    # Format for return
    results = []
    for item in selected:
        activity = item["activity"].copy()
        activity["description"] = activity["description"].replace("{child_name}", child_name)
        activity["title"] = activity["title"].replace("{child_name}", child_name)
        results.append(activity)
    
    return results



def get_developmental_milestones(stage: str, age_months: int, child_name: str) -> dict:
    """Return developmental milestones to look for based on current stage and age.
    
    Provides parents with concrete signs of progress to watch for.
    """
    
    if stage == "ALREADY WRITING":
        return {
            "intro": f"Since {child_name} is already writing real letters, here are signs of continued progress:",
            "signs": [
                f"{child_name} writing their full name with increasing accuracy",
                "Attempting to write simple words independently",
                "Asking how to spell words",
                "Writing letters or notes to family members (even if not all spellings are correct)",
                "Showing pride in their writing and wanting to share it"
            ],
            "note": f"Since {child_name} is already writing actual letters, this assessment's score is not really meaningful for them. What matters now is encouraging their love of writing and building on their strong foundation."
        }
    elif stage == "STRONG START":
        return {
            "intro": f"Since {child_name} is already showing good differentiation between writing and drawing, here are signs of continued development to look for:",
            "signs": [
                f"Letter-like shapes appearing in {child_name}'s 'writing' - these may not be real letters yet, but shapes that look like they could be",
                "Asking 'What does this say?' about print in books or on signs",
                f"{child_name} 'reading' to toys or family members by making up stories while looking at books",
                "Attempting to write their name or the first letter of their name",
                "Drawing people with more detail (faces with features, bodies with limbs)"
            ],
            "note": "These signs typically emerge over the coming months. Every child develops at their own pace."
        }
    elif stage == "BEGINNING EXPLORER":
        return {
            "intro": f"As {child_name}'s understanding develops, here are signs of progress to look for:",
            "signs": [
                f"{child_name} making their 'writing' look noticeably different from their drawings - smaller, with different colours or mark types",
                "Showing interest in letters, especially letters in their own name",
                "Asking what words say in books or on packaging",
                "'Pretend writing' that looks like rows of marks (even if not real letters)",
                "Using darker colours for 'writing' and brighter colours for pictures"
            ],
            "note": "With the activities suggested above, you may see these signs emerging over the next few months."
        }
    else:  # EARLY DAYS
        if age_months < 32:
            return {
                "intro": f"At {child_name}'s age, here are positive signs of development to look for:",
                "signs": [
                    f"{child_name} enjoying making marks with different tools (crayons, chalk, paint)",
                    "Making marks intentionally rather than randomly",
                    "Showing you their marks and wanting a response",
                    "Beginning to hold crayons with more control",
                    "Making circular scribbles (an important pre-writing skill)"
                ],
                "note": "The understanding that writing and drawing are different typically begins to emerge from around 2 years and 8 months. Right now, enjoying mark-making is the most important thing."
            }
        else:
            return {
                "intro": f"As {child_name} develops, here are signs that differentiation between writing and drawing is beginning to emerge:",
                "signs": [
                    f"Any difference at all between how {child_name} approaches 'writing' versus 'drawing' - even small differences are progress",
                    "Interest in books, especially pointing at pictures and words",
                    "Noticing print in the environment (signs, labels, packaging)",
                    "Making marks that are more controlled and deliberate",
                    "'Pretend reading' - holding a book and making up a story"
                ],
                "note": "These signs may emerge gradually over the coming months. The activities suggested above will support this development."
            }


# =============================================================================
# AGE-ADJUSTED SCORING
# =============================================================================

def get_age_adjusted_bands(age_months: int) -> dict:
    """Return scoring bands calibrated for 24-point maximum score."""
    if age_months <= 30:
        # Youngest children (24-30 months): most lenient thresholds
        return {
            "strong_start": {"min": 12, "min_percent": 50},
            "beginning_explorer": {"min": 6, "max": 11, "min_percent": 25, "max_percent": 49},
            "early_days": {"max": 5, "max_percent": 24}
        }
    elif age_months <= 42:
        # Middle age group (31-42 months): moderate thresholds
        return {
            "strong_start": {"min": 15, "min_percent": 62},
            "beginning_explorer": {"min": 9, "max": 14, "min_percent": 37, "max_percent": 61},
            "early_days": {"max": 8, "max_percent": 36}
        }
    else:
        # Oldest children (43-48 months): strictest thresholds
        return {
            "strong_start": {"min": 18, "min_percent": 75},
            "beginning_explorer": {"min": 12, "max": 17, "min_percent": 50, "max_percent": 74},
            "early_days": {"max": 11, "max_percent": 49}
        }


def determine_stage(score: int, age_months: int, child_name: str = "Your child") -> dict:
    bands = get_age_adjusted_bands(age_months)
    if score >= bands["strong_start"]["min"]:
        return {
            "stage": "STRONG START", 
            "description": f"{child_name} shows clear understanding that writing and drawing are different", 
            "detail": f"{child_name} is making distinct marks for writing versus drawing. This is excellent progress for their age."
        }
    elif score >= bands["beginning_explorer"]["min"]:
        return {
            "stage": "BEGINNING EXPLORER", 
            "description": f"{child_name} is starting to understand that writing and drawing are different", 
            "detail": f"{child_name} is beginning to show some differences between writing and drawing."
        }
    else:
        return {
            "stage": "EARLY DAYS", 
            "description": f"{child_name} is still exploring mark-making", 
            "detail": f"{child_name} is enjoying making marks on paper. The understanding that writing and drawing are different typically emerges over the coming months."
        }


def determine_stage_with_writing_stage(total_score: int, max_score: int, age_months: int, writing_stage: str, child_name: str = "Your child", blind_result: dict = None) -> dict:
    """
    Determine developmental stage using floor logic.
    
    FLOOR LOGIC: writing_stage sets MINIMUM stage
    - CONVENTIONAL → minimum STRONG START (but check short name cap)
    - EMERGING → minimum STRONG START  
    - LETTER_LIKE → minimum BEGINNING EXPLORER
    - SCRIBBLES/DRAWING → use score-based determination
    
    Differentiation score can BOOST stage up, never pull down.
    """
    
    # Get score-based stage first
    score_based = determine_stage(total_score, age_months, child_name)
    score_stage = score_based["stage"]
    
    # Define stage hierarchy (higher index = more advanced)
    stage_order = ["EARLY DAYS", "BEGINNING EXPLORER", "STRONG START", "ALREADY WRITING"]
    
    def stage_index(stage_name):
        try:
            return stage_order.index(stage_name)
        except ValueError:
            return 0
    
    # Determine floor based on writing_stage
    writing_stage_upper = writing_stage.upper() if writing_stage else ""
    
    if writing_stage_upper == "CONVENTIONAL":
        # Check for short name cap
        if blind_result:
            name_letter_count = blind_result.get('name_letter_count', 0)
            sun_readable = blind_result.get('sun_readable', False)
            
            if name_letter_count <= 3 and not sun_readable:
                # Short name without sun - cap at STRONG START
                floor_stage = "STRONG START"
            else:
                # Full CONVENTIONAL → ALREADY WRITING
                floor_stage = "ALREADY WRITING"
        else:
            floor_stage = "ALREADY WRITING"
    elif writing_stage_upper == "EMERGING":
        floor_stage = "STRONG START"
    elif writing_stage_upper == "LETTER_LIKE":
        floor_stage = "BEGINNING EXPLORER"
    else:
        # SCRIBBLES, DRAWING, or unknown - no floor, use score
        floor_stage = "EARLY DAYS"
    
    # Apply floor logic: take the HIGHER of score-based or floor
    if stage_index(floor_stage) > stage_index(score_stage):
        final_stage = floor_stage
        boost_applied = True
    else:
        final_stage = score_stage
        boost_applied = False
    
    # Build response based on final stage
    if final_stage == "ALREADY WRITING":
        return {
            "stage": "ALREADY WRITING",
            "description": f"{child_name} is writing real words!",
            "detail": f"{child_name} can write recognisable words - this is excellent progress that puts them ahead of typical development for their age.",
            "short_name_capped": False
        }
    elif final_stage == "STRONG START":
        short_name_capped = (writing_stage_upper == "CONVENTIONAL" and 
                           blind_result and 
                           blind_result.get('name_letter_count', 0) <= 3 and 
                           not blind_result.get('sun_readable', False))
        return {
            "stage": "STRONG START",
            "description": f"{child_name} shows clear understanding that writing and drawing are different",
            "detail": f"{child_name} is making distinct marks for writing versus drawing. This is excellent progress for their age.",
            "short_name_capped": short_name_capped
        }
    elif final_stage == "BEGINNING EXPLORER":
        return {
            "stage": "BEGINNING EXPLORER",
            "description": f"{child_name} is starting to understand that writing and drawing are different",
            "detail": f"{child_name} is beginning to show some differences between writing and drawing.",
            "short_name_capped": False
        }
    else:
        return {
            "stage": "EARLY DAYS",
            "description": f"{child_name} is still exploring mark-making",
            "detail": f"{child_name} is enjoying making marks on paper. The understanding that writing and drawing are different typically emerges over the coming months.",
            "short_name_capped": False
        }


def interpret_verbal_behaviour(questionnaire: dict, score: int, stage: str, child_name: str = "Your child") -> dict:
    interpretations = []
    patterns = []
    general_behaviour = questionnaire.get("general_behaviour", [])
    writing_comments = questionnaire.get("writing_comments", "").strip()
    drawing_comments = questionnaire.get("drawing_comments", "").strip()
    
    if "said_writing_hard" in general_behaviour:
        patterns.append("difficulty_writing")
        interpretations.append(f"{child_name} recognised that writing is challenging - this shows awareness that writing is different from drawing.")
    if "confident_no_comment" in general_behaviour:
        patterns.append("confident")
        interpretations.append(f"{child_name} approached both tasks confidently.")
    if "needed_encouragement" in general_behaviour:
        patterns.append("needed_encouragement")
        interpretations.append(f"{child_name} needed some encouragement, which is completely normal at this age.")
    if "enjoyed_it" in general_behaviour:
        patterns.append("enjoyed")
        interpretations.append(f"{child_name} seemed to enjoy the activities!")
    if "said_drawing_hard" in general_behaviour:
        patterns.append("difficulty_drawing")
        interpretations.append(f"{child_name} found drawing challenging too - they may still be developing confidence with mark-making.")
    if "didnt_say_much" in general_behaviour:
        patterns.append("quiet")
        interpretations.append(f"{child_name} was quietly focused on the tasks.")
    
    if writing_comments:
        skip_phrases = ["nothing", "n/a", "na", "none", "no", "did not say", "didnt say", "-", ""]
        if writing_comments.lower().strip() not in skip_phrases and len(writing_comments.strip()) > 3:
            patterns.append("writing_comment_provided")
            interpretations.append(f"While writing, you noted: \"{writing_comments}\"")
    
    if drawing_comments:
        skip_phrases = ["nothing", "n/a", "na", "none", "no", "did not say", "didnt say", "-", ""]
        if drawing_comments.lower().strip() not in skip_phrases and len(drawing_comments.strip()) > 3:
            patterns.append("drawing_comment_provided")
            interpretations.append(f"While drawing, you noted: \"{drawing_comments}\"")
    
    return {"patterns": patterns, "interpretations": interpretations}

def get_pair_comparison_interpretation(visual_scores: dict) -> str:
    pair1 = visual_scores.get("pair1_subtotal", 0)
    pair2 = visual_scores.get("pair2_subtotal", 0)
    
    # Metacognitive awareness: strong name differentiation but drew sun instead
    if pair1 >= 9 and pair2 <= 3:
        return "Your child differentiated well for their name but not for 'sun.' If they said they didn't know how to write it, that's actually positive—they understand writing should look different but need more letter knowledge."
    
    pair1_pct = (pair1 / 10) * 100
    pair2_pct = (pair2 / 11) * 100
    diff = abs(pair1_pct - pair2_pct)
    if diff < 15:
        return "Your child shows consistent differentiation across both pairs of tasks."
    elif pair1_pct > pair2_pct:
        return "Your child shows stronger differentiation with their name and self-portrait."
    else:
        return "Your child shows stronger differentiation with the sun tasks."


def get_conventional_writing_feedback(child_name: str, writing_stage_reasoning: str) -> dict:
    """Return feedback for children already writing actual letters.
    
    This assessment is designed for children who have not yet learned to write.
    If a child is already writing real letters, they have moved beyond this stage.
    """
    return {
        "stage": "ALREADY WRITING",
        "description": f"{child_name} is already writing real letters!",
        "detail": f"{child_name} has progressed beyond the early mark-making stage this assessment measures. They are already forming recognisable letters, which is wonderful! This assessment is designed for children who are still learning that writing and drawing are different - {child_name} has already mastered this concept.",
        "is_conventional": True,
        "recommendation": f"Since {child_name} is already writing, you might want to explore activities that build on their existing skills: practising letter formation, learning to write their full name, or exploring simple words. Well done!",
        "writing_stage_reasoning": writing_stage_reasoning
    }


def get_emerging_writing_feedback(child_name: str, stage_info: dict, writing_stage_reasoning: str) -> dict:
    """Enhance feedback for children showing emerging letter formation."""
    enhanced = stage_info.copy()
    enhanced["writing_development"] = f"{child_name} is starting to form some real letters! This is an exciting development."
    enhanced["writing_stage_reasoning"] = writing_stage_reasoning
    return enhanced


# =============================================================================
# JSON PARSING HELPER
# =============================================================================

def parse_json_response(ai_response: str) -> dict:
    if not ai_response:
        return None
    
    strategies = [
        lambda s: json.loads(s.strip()),
        lambda s: json.loads(s.replace("```json", "").replace("```", "").strip()),
        lambda s: json.loads(s[s.find("{"):s.rfind("}")+1]),
        lambda s: json.loads(extract_json_object(s)),
    ]
    
    for i, strategy in enumerate(strategies):
        try:
            result = strategy(ai_response)
            if isinstance(result, dict) and 'pair1_subtotal' in result or 'pair2_subtotal' in result:
                logging.info(f'JSON parsed successfully with strategy {i+1}')
                return result
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logging.debug(f'Strategy {i+1} failed: {str(e)[:100]}')
            continue
    
    logging.error(f'All JSON parsing strategies failed. Response preview: {ai_response[:300]}')
    return None


def extract_json_object(text: str) -> str:
    start = text.find('{')
    if start == -1:
        return text
    
    depth = 0
    for i, char in enumerate(text[start:], start):
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    
    return text[start:text.rfind('}')+1]


# =============================================================================
# TREIMAN RUBRIC
# =============================================================================

TREIMAN_RUBRIC = """
You are an expert scorer for early childhood writing development assessments based on Treiman & Yin (2011).

TARGET AGE: 24-42 months (2-3.5 years)

You will receive images from a young child. Score them according to the rubric below.

CRITICAL ACCURACY RULES - READ CAREFULLY:

BEFORE YOU BEGIN: Look at each image carefully. Describe ONLY what is literally visible. Do not embellish, improve, or interpret generously.

1. ONLY describe what you ACTUALLY SEE in each image - do not assume or hallucinate
2. If an image shows ONLY ONE COLOUR, state that single colour only
3. If WRITING and DRAWING images look IDENTICAL or very similar:
   - Score ALL differentiation categories as 0
   - This is important developmental information - the child has not yet learned to differentiate
   - State clearly in observations that the images are identical/similar
4. Be precise about colours: "yellow marks" not "yellow and blue" if only yellow is visible
5. Do NOT give credit for differentiation that does not exist in the images

LETTER QUALITY ACCURACY - RESEARCH-BASED FRAMEWORK:

RADICAL HONESTY: Only call a letter "recognisable" if you can DEFINITELY identify it without guessing. If uncertain, call it "letter-like mark". One clear letter is better than three unclear ones.

Example - WRONG: "recognisable letters 'O', 'b' and 'i'" (if only O is clearly visible)
Example - RIGHT: "one recognisable letter 'O' with letter-like marks"

WHICH LETTERS TO IDENTIFY:
Research shows children typically write the first letter of their name before others (Bloodgood, 1999; Puranik & Lonigan, 2011), but assess what you actually see. Always specify which letters by name. Count only letters you can DEFINITELY identify, regardless of position.

AGES 2-4 (Target age): Typical writing shows 1-3 recognisable letters (often just first letter of name), wobbly formation, inconsistent sizing, poor spacing, unsteady lines, reversals (normal). Letter-like shapes without clear identity are common and normal. Use language: "wobbly", "unsteady lines", "inconsistent sizing", "letter-like marks". Avoid: "developing", "improving".

AGES 5-7: Most name letters recognisable, minimal reversals by age 7, more consistent sizing and spacing than ages 2-4, mix of clear and unclear letters. Use: "most letters recognisable", "straighter lines", "more even spacing", "legible".

AGES 7-8+: All letters clearly identifiable, consistent formation, uniform sizing/spacing, proper alignment, no reversals, steady smooth lines, organised appearance. Use: "consistent formation", "uniform sizing", "steady lines", "all letters clearly recognisable".

CRITICAL RULES:
1. Assess only visible characteristics - no speed/fluency/motor control claims
2. For ages 2-4: reversals, wobbly lines, unclear marks are NORMAL
3. Never use "clearly formed", "neat", "well-formed" for typical 2-4 year old writing
4. Be radically honest - one clear letter is better than three unclear ones
5. One recognisable letter is an achievement for ages 2-3

VERIFY: Counted only definite letters? Specified which letters? Used only observable characteristics? Honest about quality?

SOURCES: Illinois Early Learning Project, Learning Without Tears, OFSTED Research Review, North Shore Pediatric Therapy, Growing Hands-On Kids, Bloodgood (1999), Both-de Vries & Bus (2010), Puranik & Lonigan (2011, 2012), Justice et al. (2006)


FAVOURITE COLOUR DETECTION - MANDATORY:
YOU MUST ALWAYS check for and report dominant colours in the favourite_colour_detected field.

DETECTION RULES:
1. Look at ALL samples and identify which colour(s) appear most frequently
2. If the SAME colour appears in 2+ different samples → set favourite_colour_detected to that colour name
3. If multiple colours appear equally → choose the one that appears in the most samples
4. ALWAYS set favourite_colour_detected to a specific colour name OR "none" - NEVER leave it empty

COLOUR NAMES TO USE:
- Standard colours: "black", "blue", "brown", "green", "orange", "pink", "purple", "red", "yellow"
- Custom colours the parent might mention: "turquoise", "gold", "navy", "silver", "grey", "teal", "maroon", etc.

EXAMPLES:
- Name writing uses blue + Self portrait uses blue → favourite_colour_detected = "blue"
- Name writing uses black + Self portrait uses red/green/yellow → favourite_colour_detected = "none" 
- Sun writing uses blue + Sun drawing uses blue → favourite_colour_detected = "blue"
- All 4 samples use blue → favourite_colour_detected = "blue"

Parent stated favourite colour: {favourite_colour_stated}
If this colour appears in the samples, note it. If a DIFFERENT colour dominates, report what you actually see.

PAIR 2 SPECIAL CASE - BOTH SAMPLES ARE DRAWINGS:
- If BOTH the sun writing and sun drawing samples are DRAWINGS (no attempt at letters or letter-like marks):
  - This means the child drew for both tasks because they do not know how to spell "sun"
  - Set pair2_both_are_drawings to true
  - Score Pair 2 normally but note in observations: "Both samples were drawings"
  - Explain: "The child drew for both because they do not yet know how to write the word sun, which is age-appropriate"

SCORING RULE: If the writing and drawing samples look the same (same colours, same shapes, same size, same style), ALL scores for that pair should be 0 or very low. The child showing NO differentiation is a valid and important finding.

KEY DIFFERENCES (from Treiman's research) - only score highly if these ARE present:
- WRITING: smaller, darker, more angular, sparser
- DRAWING: larger, more colourful, more curved, denser

WRITING STAGE CLASSIFICATION - CRITICAL:
You MUST classify the developmental stage of the WRITING samples (not drawing). Look at the name_writing and sun_writing images and classify:

- SCRIBBLES: Random marks with no letter-like qualities. Circular scribbles, random lines, no attempt at letter forms.
- LETTER_LIKE: Real letter shapes that do NOT represent the target word. Letters appear random or borrowed from the child's name. Example: Child writes "GMLEF" for "light" — real letters, but random. Example: Child writes letters from their own name for every word.
- EMERGING: 1-2 letters that REPRESENT the actual target word, often the first letter. Example: Clear "O" for "Obi" — the O represents their name. Example: Clear "S" for "sun" — the S represents the word.
- CONVENTIONAL: MOST or ALL letters are clearly recognisable. An adult could read the entire word without being told what it says.

LETTER_LIKE vs EMERGING - KEY DISTINCTION:
- LETTER_LIKE: Real letters but RANDOM (do not match the target word)
- EMERGING: Real letters that MATCH the target word (even if only 1-2 letters)

FIRST LETTER RESEARCH (Bloodgood, 1999; Puranik & Lonigan, 2011):
Children typically write the first letter of their name correctly before other letters. If only ONE letter is clear and it is the FIRST letter of the name, this is EMERGING — a normal developmental milestone for ages 3-4.

AGE CONTEXT (Treiman & Yin, 2011):
At ages 2-3, children rarely produce correct letters. If you classify a 2-3 year old as CONVENTIONAL, double-check your letter identification is strict.


CRITICAL DISTINCTION - READ CAREFULLY:
- If only 1-2 letters are readable → EMERGING (even if those letters are perfect)
- If most/all letters are readable → CONVENTIONAL

CRITICAL - PREVENT CONFIRMATION BIAS:

You are told the child's name, but DO NOT use it to interpret what you see. You must identify letters as if you DO NOT KNOW the child's name.

WRONG approach: "Child is Olivia, I see marks, these must be O-l-i-v-a"
RIGHT approach: "I see one clear circular letter 'O'. The remaining marks are unclear and I cannot confidently identify them as specific letters."

TEST YOURSELF:
- Cover the child's name mentally
- Look at ONLY the marks on the page
- Which letters can you DEFINITELY identify without knowing the name?
- If you need the name to "see" the letters, they are NOT clearly readable

COMMON CONFIRMATION BIAS ERRORS:
- "I see O, l, i" when only "O" is actually clear (because you know the name is Oli/Olivia/Obi)
- "I see S, a, m" when only "S" is clear (because you know the name is Sam)
- Counting wobbly marks as letters because they COULD match the expected name
- Being generous because you want the letters to spell the name

BE STRICT:
- Only count letters you could identify WITHOUT knowing the name
- One clear letter + unclear marks = EMERGING, not CONVENTIONAL
- If you are unsure about a letter, it does NOT count as recognisable
- "Possibly an 'l'" means it is NOT clearly recognisable

LETTER COUNT CHECK:
After identifying letters, ask: "Would a stranger who doesn't know this child's name read these same letters?"
- If YES for most/all letters → CONVENTIONAL
- If YES for only 1-2 letters → EMERGING
- If NO for all letters → LETTER_LIKE or SCRIBBLES


- When in doubt, choose EMERGING over CONVENTIONAL
- Short names (2-3 letters) need ALL letters clear for CONVENTIONAL
- Longer names (4+ letters) need MOST letters clear for CONVENTIONAL

EXAMPLE CLASSIFICATIONS:

EMERGING examples (1-2 clear letters only):
- "Obi" where only "O" is clearly a letter, "b" and "i" are wobbly marks → EMERGING
- "Sam" where "S" is clear but "am" are scribbles → EMERGING
- "Maya" where "M" and "a" are recognisable but "ya" are unclear → EMERGING
- "Tom" where "T" is clear but "om" are joined/unclear → EMERGING
- Any name where you can only confidently identify 1-2 letters → EMERGING
- First letter perfect, rest are attempts → EMERGING
- Some letters reversed or backwards but recognisable → EMERGING

CONVENTIONAL examples (most/all letters readable):
- "Yvette" where all 6 letters can be identified → CONVENTIONAL
- "Sam" where S, a, and m are all clearly readable → CONVENTIONAL
- "Obi" where O, b, and i are all identifiable (even if wobbly) → CONVENTIONAL
- "sun" where s, u, and n are all readable → CONVENTIONAL
- "Maya" where all 4 letters can be read → CONVENTIONAL
- Letters may be wobbly/uneven but an adult can read the whole word → CONVENTIONAL

NOT CONVENTIONAL (common mistakes to avoid):
- Stick figure with circular head is NOT the letter "O"
- Drawing that happens to contain circular shapes is NOT letter writing
- Marks that COULD be letters if you squint are NOT conventional writing
- One perfect letter + scribbles = EMERGING, not CONVENTIONAL
- Being generous about unclear letters = WRONG, be strict

ASK YOURSELF:
1. Could a stranger read this word without being told what it says?
2. How many letters can I DEFINITELY identify?
3. Am I being generous or strict? (Be strict)

If a stranger could not read the word → NOT CONVENTIONAL
If you can only identify 1-2 letters with certainty → EMERGING
If most/all letters are readable by anyone → CONVENTIONAL

THIS IS CRITICAL: If you see real letter writing (real, readable letters like proper alphabet letters), you MUST classify it as CONVENTIONAL. This indicates the child is BEYOND the target developmental stage for this assessment.

Signs of real letter writing:
- Clearly readable letters (A, B, C, etc.)
- Proper letter formation
- Could be read by any adult
- Looks like actual handwriting, not scribbles or attempts

SCORING RUBRIC (24 points total for full assessment):

PAIR 1: NAME vs SELF-PORTRAIT (10 points)
1. SIZE DIFFERENCE (0-3): 3=name significantly smaller, 2=moderately smaller, 1=slightly smaller, 0=same size or larger
2. COLOUR DIFFERENTIATION (0-2): 2=name uses dark colour (black/grey/pencil) while portrait uses colours, 1=both use same colours, 0=name uses bright colours while portrait uses dark (reverse of expected)
3. ANGULARITY (0-2): 2=clear difference in mark types, 1=moderate, 0=same mark types
4. DENSITY (0-2): 2=name clearly sparser, 1=moderate, 0=same density
5. SHAPE FEATURES (0-1): 1=portrait has recognisable features + name has letter-like shapes, 0=similar appearance

PAIR 2: SUN WRITING vs SUN DRAWING (11 points)
6. SIZE DIFFERENCE (0-3): Same as above - 0 if same size
7. COLOUR + OBJECT-APPROPRIATE (0-3): 3=writing dark + drawing yellow/orange, 2=writing dark + drawing coloured, 1=slight difference, 0=SAME colours in both (score 0 if both are yellow)
8. ANGULARITY (0-2): Same as above - 0 if same mark types
9. DENSITY (0-2): Same as above - 0 if same density
10. SHAPE FEATURES (0-1): 1=drawing is sun-shaped + writing is letter-like, 0=both look similar

CROSS-PAIR (3 points) - Only if both pairs provided
11. WRITING CONSISTENCY (0-2): 2=both writing samples similar style, 1=moderate, 0=different
12. DRAWING VARIETY (0-1): 1=portrait and sun look appropriately different, 0=similar

OBSERVATIONS RULES:
- Describe EXACTLY and ONLY what you see in each image
- Start each observation by stating what the child was asked to do: "The child produced..."
- Do NOT use "ink" to describe marks. Use: "pencil", "pen", "crayon", or just the colour (e.g., "in black", "using black pencil")
- DOTS AND SPECKS ARE NOT LETTERS: Small scattered dots are SCRIBBLES, not letter-like marks. Letter-like marks must have clear line structure.
- If images are identical, say "Identical to [other image]" or "Same as [other image]"
- NEVER LIST SPECIFIC LETTERS BY NAME in observations. Instead describe:
  - "recognisable letter forms" or "clearly formed letters"
  - "three distinct letters" or "several letter shapes"
  - Example GOOD: "The child produced recognisable letter forms in black pencil"
  - Example BAD: "The child wrote O, A, and b"
- The parent can see the uploaded image - your job is to assess development level, NOT to transcribe the writing
- Focus on: colour used, size, mark quality (wobbly/steady), whether letters are recognisable as a group

MANDATORY LETTER COUNT:
Before classifying writing_stage, you MUST count letters honestly:

1. letters_identified = letters you are 100% CERTAIN about
2. letters_uncertain = letters you THINK might be there but are not sure
3. Only count CERTAIN letters when deciding the classification

RULE:
- Certain letters < half the name → EMERGING (maximum)
- Certain letters >= half the name → Could be CONVENTIONAL

EXAMPLE - Name is "Obi" (3 letters):
- You see: clear "O", wobbly marks for "b" and "i"
- letters_identified: ["O"]
- letters_uncertain: ["b", "i"]
- Certain count: 1 out of 3 = 33%
- 33% is less than half → EMERGING (not CONVENTIONAL)

EXAMPLE - Name is "Yvette" (6 letters):
- You see: all 6 letters clearly readable
- letters_identified: ["Y", "v", "e", "t", "t", "e"]
- letters_uncertain: []
- Certain count: 6 out of 6 = 100%
- 100% is more than half → CONVENTIONAL

CRITICAL: Respond with ONLY a valid JSON object. No explanations, no markdown, no text before or after the JSON. Start your response with { and end with }.

JSON FORMAT:
{"writing_stage":"SCRIBBLES|LETTER_LIKE|EMERGING|CONVENTIONAL","writing_stage_reasoning":"Explain what you see in the writing samples that led to this classification","letters_identified":["list only letters you are 100% certain about"],"letters_uncertain":["list letters you think might be there but unsure"],"pair1_scores":{"size_difference":{"score":0,"max":3,"reasoning":""},"colour_differentiation":{"score":0,"max":2,"reasoning":""},"angularity":{"score":0,"max":2,"reasoning":""},"density":{"score":0,"max":2,"reasoning":""},"shape_features":{"score":0,"max":1,"reasoning":""}},"pair1_subtotal":0,"pair2_scores":{"size_difference":{"score":0,"max":3,"reasoning":""},"colour_object_appropriate":{"score":0,"max":3,"reasoning":""},"angularity":{"score":0,"max":2,"reasoning":""},"density":{"score":0,"max":2,"reasoning":""},"shape_features":{"score":0,"max":1,"reasoning":""}},"pair2_subtotal":0,"pair2_both_are_drawings":false,"cross_pair_scores":{"writing_consistency":{"score":0,"max":2,"reasoning":""},"drawing_variety":{"score":0,"max":1,"reasoning":""}},"cross_pair_subtotal":0,"total_score":0,"max_score":24,"observations":{"name_writing":"","self_portrait":"","sun_writing":"","sun_drawing":""},"favourite_colour_detected":"none or colour name","strongest_evidence":"","areas_for_development":""}
For PARTIAL assessments (only one pair), set the missing pair's scores to null and subtotal to 0.
"""


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route(route="health")
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Health check endpoint called.')
    return func.HttpResponse(
        json.dumps({
            "status": "healthy",
            "message": "Early Writing Starter API is running!",
            "version": "7.7.0",
            "max_score": 24,
            "features": ["writing_stage_detection", "conventional_writer_handling", "email_tracking"],
            "services": {
                "openai": "available" if OPENAI_AVAILABLE else "not installed",
                "storage": "available" if STORAGE_AVAILABLE else "not installed",
                "cosmos_db": "available" if COSMOS_AVAILABLE else "not installed",
                "pdf_generation": "available" if REPORTLAB_AVAILABLE else "not installed",
                "email": "available" if EMAIL_AVAILABLE else "not installed"
            },
            "partial_assessments": "supported"
        }),
        mimetype="application/json",
        status_code=200
    )


# =============================================================================
# SESSION TOKEN MANAGEMENT (for "Do It Later" feature)
# =============================================================================

def get_sessions_container():
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("sessions")
    return container


@app.route(route="store_session", methods=["POST"])
def store_session(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Store session endpoint called.')
    
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Database not available"}), mimetype="application/json", status_code=503)
    
    try:
        req_body = req.get_json()
        session_token = req_body.get('session_token')
        
        if not session_token:
            return func.HttpResponse(json.dumps({"error": "session_token required"}), mimetype="application/json", status_code=400)
        
        container = get_sessions_container()
        if not container:
            return func.HttpResponse(json.dumps({"error": "Sessions container not available"}), mimetype="application/json", status_code=503)
        
        expiry_date = datetime.utcnow() + timedelta(days=30)
        
        session_data = {
            "id": session_token,
            "session_token": session_token,
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": expiry_date.isoformat(),
            "used": False,
            "is_test": req_body.get('is_test', False),
            "ttl": 30 * 24 * 60 * 60
        }
        
        container.upsert_item(body=session_data)
        logging.info(f'Session stored: {session_token}')
        
        return func.HttpResponse(json.dumps({"success": True}), mimetype="application/json", status_code=200)
        
    except Exception as e:
        logging.error(f'Error storing session: {str(e)}')
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


@app.route(route="verify_session", methods=["GET"])
def verify_session(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Verify session endpoint called.')
    
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"valid": False, "error": "Database not available"}), mimetype="application/json", status_code=200)
    
    try:
        token = req.params.get('token')
        
        if not token:
            return func.HttpResponse(json.dumps({"valid": False, "error": "token required"}), mimetype="application/json", status_code=200)
        
        container = get_sessions_container()
        if not container:
            return func.HttpResponse(json.dumps({"valid": False, "error": "Sessions container not available"}), mimetype="application/json", status_code=200)
        
        try:
            session = container.read_item(item=token, partition_key=token)
            
            expires_at = datetime.fromisoformat(session.get('expires_at', '2000-01-01'))
            if datetime.utcnow() > expires_at:
                return func.HttpResponse(json.dumps({"valid": False, "error": "Session expired"}), mimetype="application/json", status_code=200)
            
            return func.HttpResponse(json.dumps({"valid": True}), mimetype="application/json", status_code=200)
            
        except exceptions.CosmosResourceNotFoundError:
            return func.HttpResponse(json.dumps({"valid": False, "error": "Session not found"}), mimetype="application/json", status_code=200)
        
    except Exception as e:
        logging.error(f'Error verifying session: {str(e)}')
        return func.HttpResponse(json.dumps({"valid": False, "error": str(e)}), mimetype="application/json", status_code=200)


@app.route(route="send_access_link", methods=["POST"])
def send_access_link(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Send access link endpoint called.')
    
    if not EMAIL_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Email not available"}), mimetype="application/json", status_code=503)
    
    try:
        req_body = req.get_json()
        recipient_email = req_body.get('recipient_email')
        access_link = req_body.get('access_link')
        
        if not recipient_email or not access_link:
            return func.HttpResponse(json.dumps({"error": "recipient_email and access_link required"}), mimetype="application/json", status_code=400)
        
        email_client = get_email_client()
        if not email_client:
            return func.HttpResponse(json.dumps({"error": "Email client not available"}), mimetype="application/json", status_code=503)
        
        subject = "Your Early Writing Starter Link"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h1 style="color: #1B75BC;">Your Early Writing Starter</h1>
                <p>Thank you for your purchase!</p>
                <p>When you are ready to do the activity with your child, click the button below:</p>
                <p style="margin: 30px 0;">
                    <a href="{access_link}" style="background: #1B75BC; color: white; padding: 15px 30px; text-decoration: none; border-radius: 8px; font-weight: bold;">Start Activity</a>
                </p>
                <p>Or copy this link: <br/><a href="{access_link}">{access_link}</a></p>
                <p><strong>This link is valid for 30 days.</strong></p>
                <hr style="border: none; border-top: 1px solid #ccc; margin: 30px 0;" />
                <p style="font-size: 14px; color: #666;">
                    <strong>Before you start, make sure you have:</strong><br/>
                    • 10–15 minutes of quiet time<br/>
                    • 4 sheets of paper<br/>
                    • Crayons or coloured pencils<br/>
                    • A pencil or pen<br/>
                    • Your child ready to draw and write
                </p>
                <hr style="border: none; border-top: 1px solid #ccc; margin: 30px 0;" />
                <p style="font-size: 12px; color: #666;">
                    Questions? Contact us at <a href="mailto:contact@morehandwriting.co.uk">contact@morehandwriting.co.uk</a><br/><br/>
                    © More Handwriting | <a href="https://morehandwriting.co.uk">morehandwriting.co.uk</a>
                </p>
            </div>
        </body>
        </html>
        """
        
        message = {
            "senderAddress": "DoNotReply@morehandwriting.co.uk",
            "recipients": {"to": [{"address": recipient_email}]},
            "content": {"subject": subject, "html": html_content}
        }
        
        poller = email_client.begin_send(message)
        result = poller.result()
        
        return func.HttpResponse(json.dumps({"success": True, "message_id": result.get("id", "")}), mimetype="application/json", status_code=200)
        
    except Exception as e:
        logging.error(f'Error sending access link: {str(e)}')
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# UPLOAD IMAGES
# =============================================================================

@app.route(route="upload_images", methods=["POST"])
def upload_images(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Upload images endpoint called.')
    
    if not STORAGE_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
    
    try:
        req_body = req.get_json()
        images = req_body.get('images', {})

        # Get the parent's stated favourite colour
        questionnaire = req_body.get('questionnaire', {})
        favourite_colour_stated = questionnaire.get('favourite_colour', 'not specified')

        # Get the parent's stated favourite colour
        questionnaire = req_body.get('questionnaire', {})
        favourite_colour_stated = questionnaire.get('favourite_colour', 'not specified')
        pair1_complete = bool(images.get('name_writing') and images.get('self_portrait'))
        pair2_complete = bool(images.get('sun_writing') and images.get('sun_drawing'))
        
        if not pair1_complete and not pair2_complete:
            return func.HttpResponse(json.dumps({"error": "At least one complete pair required"}), mimetype="application/json", status_code=400)
        
        assessment_id = str(uuid.uuid4())
        image_urls = {}
        
        for image_name, image_base64 in images.items():
            if image_base64:
                image_data = base64.b64decode(image_base64)
                url = upload_image_to_blob(assessment_id, image_name, image_data)
                image_urls[image_name] = url
        
        return func.HttpResponse(json.dumps({"success": True, "assessment_id": assessment_id, "image_urls": image_urls}), mimetype="application/json", status_code=200)
    except Exception as e:
        logging.error(f'Error uploading images: {str(e)}')
        return func.HttpResponse(json.dumps({"error": "Failed to upload images", "details": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# SAVE/GET ASSESSMENT
# =============================================================================

@app.route(route="save_assessment", methods=["POST"])
def save_assessment(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Save assessment endpoint called.')
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Cosmos DB not available"}), mimetype="application/json", status_code=503)
    try:
        req_body = req.get_json()
        if not req_body.get('assessment_id'):
            return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
        result = save_assessment_to_db(req_body)
        return func.HttpResponse(json.dumps({"success": True, "assessment_id": result.get("assessment_id")}), mimetype="application/json", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


@app.route(route="get_assessment", methods=["GET"])
def get_assessment(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Get assessment endpoint called.')
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Cosmos DB not available"}), mimetype="application/json", status_code=503)
    try:
        assessment_id = req.params.get('assessment_id')
        if not assessment_id:
            return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
        assessment = get_assessment_from_db(assessment_id)
        if not assessment:
            return func.HttpResponse(json.dumps({"error": "Not found"}), mimetype="application/json", status_code=404)
        return func.HttpResponse(json.dumps(assessment), mimetype="application/json", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# GENERATE/GET REPORT
# =============================================================================

@app.route(route="generate_report", methods=["POST"])
def generate_report(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Generate report endpoint called.')
    if not REPORTLAB_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "ReportLab not available"}), mimetype="application/json", status_code=503)
    try:
        req_body = req.get_json()
        assessment_data = req_body.get('assessment_data')
        if not assessment_data:
            assessment_id = req_body.get('assessment_id')
            if assessment_id and COSMOS_AVAILABLE:
                assessment_data = get_assessment_from_db(assessment_id)
        if not assessment_data:
            return func.HttpResponse(json.dumps({"error": "assessment_data or assessment_id required"}), mimetype="application/json", status_code=400)
        
        pdf_bytes = generate_assessment_pdf(assessment_data)
        
        if req_body.get('return_pdf', False):
            return func.HttpResponse(pdf_bytes, mimetype="application/pdf", status_code=200)
        
        if STORAGE_AVAILABLE:
            report_id = assessment_data.get('assessment_id', str(uuid.uuid4()))
            pdf_url = upload_pdf_to_blob(report_id, pdf_bytes)
            return func.HttpResponse(json.dumps({"success": True, "pdf_url": pdf_url}), mimetype="application/json", status_code=200)
        
        return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


@app.route(route="get_report", methods=["GET"])
def get_report(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Get report endpoint called.')
    if not STORAGE_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
    try:
        assessment_id = req.params.get('assessment_id')
        if not assessment_id:
            return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
        pdf_bytes = get_pdf_from_blob(assessment_id)
        return func.HttpResponse(pdf_bytes, mimetype="application/pdf", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# SEND EMAIL
# =============================================================================

@app.route(route="send_email", methods=["POST"])
def send_email(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Send email endpoint called.')
    if not EMAIL_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Email not available"}), mimetype="application/json", status_code=503)
    try:
        req_body = req.get_json()
        recipient_email = req_body.get('recipient_email')
        if not recipient_email:
            return func.HttpResponse(json.dumps({"error": "recipient_email required"}), mimetype="application/json", status_code=400)
        
        assessment_data = req_body.get('assessment_data')
        if not assessment_data:
            assessment_id = req_body.get('assessment_id')
            if assessment_id and COSMOS_AVAILABLE:
                assessment_data = get_assessment_from_db(assessment_id)
        
        if not assessment_data:
            return func.HttpResponse(json.dumps({"error": "assessment_data or assessment_id required"}), mimetype="application/json", status_code=400)
        
        child_name = assessment_data.get('child', {}).get('name', 'Your Child')
        assessment_id = assessment_data.get('assessment_id', str(uuid.uuid4()))
        
        pdf_bytes = None
        if REPORTLAB_AVAILABLE:
            try:
                pdf_bytes = generate_assessment_pdf(assessment_data)
            except Exception as e:
                logging.warning(f'Failed to generate PDF: {str(e)}')
        
        result = send_assessment_email(recipient_email, child_name, assessment_id, pdf_bytes)
        return func.HttpResponse(json.dumps({"success": True, "email_status": result}), mimetype="application/json", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)

# =============================================================================
# BLIND STRANGER TEST - Detects conventional writers without confirmation bias
# =============================================================================

def blind_stranger_test(client, deployment_name, name_image: str = None, sun_image: str = None) -> dict:
    """
    Blind read test with fluent characteristic detection.
    
    CONVENTIONAL shortcut ONLY if:
    - Sun readable as "sun" (proves phonetic spelling), OR
    - Name has 4+ readable letters AND shows fluent characteristics
    
    RULES:
    1. ≤3 letter names CANNOT shortcut to CONVENTIONAL (not enough to assess fluency)
    2. Sun readable always triggers CONVENTIONAL (proves phonetic spelling)
    3. 4+ letter names need fluency check to shortcut without sun
    """
    content = [{"type": "text", "text": "Look at each image of a child's writing."}]
    
    labels = []
    if name_image:
        content.append({"type": "text", "text": "Image 1 (name writing):"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{name_image}"}})
        labels.append("name")
    if sun_image:
        content.append({"type": "text", "text": "Image 2 (sun writing):"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{sun_image}"}})
        labels.append("sun")
    
    if not labels:
        return {"is_established": False, "error": "No images provided"}
    
    try:
        response = client.chat.completions.create(
            model=deployment_name,
            messages=[{
                "role": "system",
                "content": """You assess whether a child's writing is readable AND shows fluent characteristics.

PART 1: READABILITY
Assess each sample honestly. Can you read what it says?

IMPORTANT - WOBBLY DOES NOT MEAN UNREADABLE:
- Young children's writing is often wobbly, uneven, or written with crayon - this does NOT mean unreadable
- If you can identify what the word says, it IS readable even if the strokes are imperfect
- Do NOT reject readable writing just because it looks childish or messy

SAY "I DON'T KNOW" ONLY if:
- The marks are random scribbles with no letter shapes
- You genuinely cannot tell what letters are intended
- The marks look like drawings, not writing attempts

STATE THE WORD if:
- You can identify what the word says (even if wobbly or messy)
- The letters are recognisable as specific letters
- A reasonable person would read it the same way

EXAMPLES:
- Wobbly "sun" in crayon → readable, state "sun"
- Messy "Bukunmi" with uneven letters → readable, state "Bukunmi"  
- Random scribbles with no letter shapes → "I DON'T KNOW"
- Circular marks that could be anything → "I DON'T KNOW"

PART 2: FLUENT CHARACTERISTICS (for name writing only)
If the name IS readable AND has 4 or more letters, assess whether it shows FLUENT writing characteristics.

FLUENT writing (typical of children age 5+ or advanced writers) shows ALL of:
- Consistent letter size (all letters similar height)
- Baseline adherence (letters sit on an invisible line)
- Smooth strokes (not wobbly or broken)
- Consistent pressure (all letters similar darkness)
- Proportional spacing (even gaps between letters)

NON-FLUENT writing (typical of ages 2-4) shows ANY of:
- Inconsistent letter size (some big, some small)
- No baseline (letters float at different heights)
- Wobbly strokes (unsteady lines)
- Uneven pressure (some letters darker or fainter than others)
- Irregular spacing (cramped or scattered)

IMPORTANT FOR SHORT NAMES (3 letters or fewer):
- You CANNOT assess fluency with only 2-3 letters
- Not enough data points to judge consistency
- Set fluent to false and reasoning to "Cannot assess fluency with X letters"

BE VERY STRICT about fluent characteristics:
- If even ONE feature is non-fluent, set fluent to false
- Most 2-4 year olds do NOT show fluent characteristics even if their letters are readable
- A readable but wobbly "OAb" is NOT fluent
- A perfectly formed "Yvette" with consistent size/baseline IS fluent

Also note the main colour used.

Reply ONLY with JSON:
{
  "image1_word": "word or I DON'T KNOW",
  "image1_colour": "colour",
  "image1_fluent": true or false,
  "image1_fluent_reasoning": "brief explanation of why fluent or not",
  "image2_word": "word or I DON'T KNOW", 
  "image2_colour": "colour"
}

If only one image, omit image2 fields.
Set image1_fluent to false if image1_word is "I DON'T KNOW"."""
            }, {
                "role": "user",
                "content": content
            }],
            max_tokens=300,
            temperature=0.0
        )
        
        result_text = response.choices[0].message.content
        logging.info(f"Blind stranger test raw: {result_text}")
        
        # Parse JSON response
        try:
            result = json.loads(result_text.replace("```json", "").replace("```", "").strip())
        except:
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(0))
            else:
                return {"is_established": False, "error": "Could not parse response"}
        
        name_word = result.get('image1_word')
        name_colour = result.get('image1_colour', '')
        name_fluent = result.get('image1_fluent', False)
        name_fluent_reasoning = result.get('image1_fluent_reasoning', '')
        sun_word = result.get('image2_word')
        sun_colour = result.get('image2_colour', '')
        
        # Check if readable
        not_readable_values = [
            "I DON'T KNOW", "I DONT KNOW", "IDK", "UNKNOWN", "UNCLEAR", 
            "NULL", "NONE", "N/A", "NA", "", "CAN'T READ", "CANT READ",
            "NOT SURE", "UNSURE", "ILLEGIBLE", "UNREADABLE"
        ]
        
        name_readable = name_word and str(name_word).upper().strip() not in not_readable_values
        
        # Check if sun is readable - strip punctuation and whitespace
        sun_word_clean = str(sun_word).upper().strip().rstrip('.,!?') if sun_word else ""
        sun_readable = sun_word_clean == "SUN"
        
        # Count letters in name
        name_letter_count = len(str(name_word).strip()) if name_readable else 0
        
        # Cannot assess fluency with ≤3 letters
        if name_letter_count <= 3:
            name_fluent = False
            name_fluent_reasoning = f"Cannot assess fluency with only {name_letter_count} letters"
        
        # Ensure fluent is False if not readable
        if not name_readable:
            name_fluent = False
            name_fluent_reasoning = "Not readable"
        
        # CONVENTIONAL shortcut logic
        is_established = (
            sun_readable or 
            (name_readable and name_letter_count >= 4 and name_fluent)
        )
        
        logging.info(f"Blind test decision: sun_readable={sun_readable}, name_readable={name_readable}, "
                    f"name_word={name_word}, name_letter_count={name_letter_count}, "
                    f"name_fluent={name_fluent}, is_established={is_established}")
        
        return {
            "is_established": is_established,
            "name_readable": name_readable,
            "name_word": name_word if name_readable else None,
            "name_letter_count": name_letter_count,
            "name_colour": name_colour,
            "name_fluent": name_fluent,
            "name_fluent_reasoning": name_fluent_reasoning,
            "sun_readable": sun_readable,
            "sun_word": sun_word if sun_readable else None,
            "sun_colour": sun_colour
        }
        
    except Exception as e:
        logging.error(f"Blind stranger test failed: {str(e)}")
        return {"is_established": False, "error": str(e)}

# =============================================================================
# SCORE ASSESSMENT - MAIN ENDPOINT
# =============================================================================

# =============================================================================
# TRIPLE AI VERIFICATION - HELPER FUNCTIONS
# =============================================================================

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

def extract_letters_from_text(text):
    """
    Extract letter mentions from observation text.
    Looks for patterns like 'O', "recognisable letter 'A'", etc.
    """
    if not text:
        return []
    
    # Find letters mentioned in quotes or parentheses
    patterns = [
        r"letter[s]?\s+['\"]([A-Za-z])['\"]",  # letter 'A'
        r"letter[s]?\s+\(([A-Za-z])\)",         # letter (A)
        r"['\"]([A-Z])['\"]",                    # 'O' or "O"
        r"\(([A-Z])\)",                          # (O)
    ]
    
    letters = []
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        letters.extend([m.upper() for m in matches])
    
    # Remove duplicates while preserving order
    seen = set()
    unique_letters = []
    for letter in letters:
        if letter not in seen:
            seen.add(letter)
            unique_letters.append(letter)
    
    return unique_letters

def compare_three_assessments(assessments):
    """
    Compare 3 AI assessments and build consensus.
    Checks scores, letters and observations for agreement.
    Returns merged assessment with averaged scores and consensus observations.
    """
    # Extract letters identified in name_writing observations
    letters_by_ai = []
    for assessment in assessments:
        obs = assessment.get('observations', {}).get('name_writing', '')
        letters = extract_letters_from_text(obs)
        letters_by_ai.append(letters)
    
    # Count agreement for each letter
    all_letters = set()
    for letters in letters_by_ai:
        all_letters.update(letters)
    
    letter_consensus = {}
    for letter in all_letters:
        count = sum(1 for letters in letters_by_ai if letter in letters)
        letter_consensus[letter] = count
    
    # Filter: only keep letters seen by 2+ AIs
    agreed_letters = [letter for letter, count in letter_consensus.items() if count >= 2]
    
    # Extract scores from all assessments
    pair1_totals = []
    pair2_totals = []
    cross_totals = []
    total_scores = []
    
    for assessment in assessments:
        pair1_totals.append(assessment.get('pair1_subtotal', 0))
        pair2_totals.append(assessment.get('pair2_subtotal', 0))
        cross_totals.append(assessment.get('cross_pair_subtotal', 0))
        total_scores.append(assessment.get('total_score', 0))
    
    # Check for large score discrepancies
    total_range = max(total_scores) - min(total_scores)
    if total_range > 3:
        logging.warning(f"Large score discrepancy detected: scores range from {min(total_scores)} to {max(total_scores)}")
    
    # Use median score
    def median(values):
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n % 2 == 0:
            return (sorted_vals[n//2-1] + sorted_vals[n//2]) / 2
        return sorted_vals[n//2]
    
    consensus_pair1 = round(median(pair1_totals))
    consensus_pair2 = round(median(pair2_totals))
    consensus_cross = round(median(cross_totals))
    consensus_total = round(median(total_scores))
    
    # Find assessment with total score closest to consensus
    best_idx = 0
    smallest_diff = abs(total_scores[0] - consensus_total)
    
    for idx, score in enumerate(total_scores):
        diff = abs(score - consensus_total)
        if diff < smallest_diff:
            smallest_diff = diff
            best_idx = idx
    
    # Use this assessment as base
    consensus_assessment = assessments[best_idx].copy()
    
    # Update totals with consensus scores
    consensus_assessment['pair1_subtotal'] = consensus_pair1
    consensus_assessment['pair2_subtotal'] = consensus_pair2
    consensus_assessment['cross_pair_subtotal'] = consensus_cross
    consensus_assessment['total_score'] = consensus_total
    
    # SMART CONSENSUS NOTE - Only add when appropriate
    writing_stage = consensus_assessment.get('writing_stage', '').upper()
    is_conventional = writing_stage == 'CONVENTIONAL'
    is_emerging = writing_stage == 'EMERGING'
    
    # Only add consensus note for non-conventional writers
    if not is_conventional and not is_emerging:
        obs = consensus_assessment.get('observations', {}).get('name_writing', '')
        if agreed_letters:
            consensus_note = f"[Verified letters: {', '.join(agreed_letters)}]"
        else:
            consensus_note = "[Multiple assessments found no clearly recognisable letters]"
        
        if obs:
            consensus_assessment['observations']['name_writing'] = f"{obs} {consensus_note}"
    
    logging.info(f"Consensus scores - Pair1: {consensus_pair1}, Pair2: {consensus_pair2}, Cross: {consensus_cross}, Total: {consensus_total}")
    logging.info(f"Score range was {min(total_scores)}-{max(total_scores)}, selected assessment {best_idx+1}")
    
    return consensus_assessment


       

def call_ai_once(client, deployment_name, personalized_rubric, user_content):
    """
    Make a single AI API call. Used for parallel execution.
    """
    try:
        response = client.chat.completions.create(
            model=deployment_name,
            messages=[
                {"role": "system", "content": personalized_rubric},
                {"role": "user", "content": user_content}
            ],
            max_tokens=2000,
            temperature=0.2
        )
        
        ai_response = response.choices[0].message.content
        logging.info(f"Got response length: {len(ai_response)}")
        
        return parse_json_response(ai_response)
        
    except Exception as e:
        logging.error(f"AI call failed: {str(e)}")
        return None


@app.route(route="score_assessment", methods=["GET", "POST"])
def score_assessment(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Score assessment endpoint called.')
    
    
    # Admin mode: /api/score_assessment?mode=admin&action=stats&admin_password=xxx
    # Works for both GET and POST requests
    if req.params.get("mode") == "admin":
        return _handle_admin(req)
    
    if req.method == "GET":
        return func.HttpResponse(
            json.dumps({
                "endpoint": "score_assessment",
                "method": "POST",
                "description": "Score a child's writing/drawing assessment",
                "partial_assessments": "supported - minimum one complete pair"
            }),
            mimetype="application/json",
            status_code=200
        )
    try:
        if not OPENAI_AVAILABLE:
            return func.HttpResponse(json.dumps({"error": "OpenAI not available"}), mimetype="application/json", status_code=503)
        
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        deployment_name = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-2")
        
        if not api_key or not endpoint:
            return func.HttpResponse(json.dumps({"error": "Missing Azure OpenAI configuration"}), mimetype="application/json", status_code=503)
        
        client = AzureOpenAI(api_key=api_key, api_version="2024-10-21", azure_endpoint=endpoint)
        
        try:
            req_body = req.get_json()
        except ValueError:
            return func.HttpResponse(json.dumps({"error": "Invalid JSON"}), mimetype="application/json", status_code=400)
        
        if not req_body:
            return func.HttpResponse(json.dumps({"error": "Request body required"}), mimetype="application/json", status_code=400)
        
        images = req_body.get('images', {})
        
        pair1_complete = bool(images.get('name_writing') and images.get('self_portrait'))
        pair2_complete = bool(images.get('sun_writing') and images.get('sun_drawing'))
        
        if not pair1_complete and not pair2_complete:
            return func.HttpResponse(
                json.dumps({"error": "At least one complete pair required", "received": list(images.keys())}),
                mimetype="application/json",
                status_code=400
            )
        
        child_name = req_body.get('child_name', 'the child')
        child_age_months = req_body.get('child_age_months', 36)
        assessment_id = req_body.get('assessment_id', str(uuid.uuid4()))
        generate_pdf = req_body.get('generate_pdf', False)
        questionnaire = req_body.get('questionnaire', {})
        recipient_email = req_body.get('email', '')
        favourite_colour_stated = questionnaire.get('favourite_colour', '')
        
        # Detect test sessions
        is_test_session = False
        session_token = req_body.get('session_token', '')
        if session_token:
            try:
                sessions_container = get_sessions_container()
                if sessions_container:
                    session_doc = sessions_container.read_item(item=session_token, partition_key=session_token)
                    is_test_session = session_doc.get('is_test', False)
            except Exception:
                pass
        if not is_test_session and session_token.startswith('test_free_'):
            is_test_session = True
        
        if child_age_months < 24 or child_age_months > 48:
            return func.HttpResponse(json.dumps({"error": "Age must be 24-48 months"}), mimetype="application/json", status_code=400)
        
        age_years = child_age_months // 12
        age_months_remainder = child_age_months % 12
        
        logging.info(f'Scoring for {child_name}, age {age_years}y {age_months_remainder}m, pair1={pair1_complete}, pair2={pair2_complete}, email={recipient_email}')
        
        # Store images for debugging (auto-deleted after 7 days via Azure lifecycle policy)
        try:
            for image_name, image_base64 in images.items():
                if image_base64:
                    image_data = base64.b64decode(image_base64)
                    upload_image_to_blob(assessment_id, image_name, image_data)
            logging.info(f'Images stored for assessment {assessment_id}')
        except Exception as e:
            logging.warning(f'Failed to store images: {str(e)}')
      
        # Build favourite colour context for AI
        fav_colour_context = ""
        if favourite_colour_stated and favourite_colour_stated not in ['', 'no_preference']:
            fav_colour_context = f"\n\nThe parent stated that {child_name}'s favourite colour is: {favourite_colour_stated}"
        
        if pair1_complete and pair2_complete:
            prompt_text = f"""Please score this assessment for {child_name}, age {age_years} years and {age_months_remainder} months.

The 4 images are in order: NAME_WRITING, SELF_PORTRAIT, SUN_WRITING, SUN_DRAWING.{fav_colour_context}

Apply the Treiman rubric and return scores in the specified JSON format."""
        elif pair1_complete:
            prompt_text = f"""Please score this PARTIAL assessment for {child_name}, age {age_years} years and {age_months_remainder} months.

Only 2 images (Pair 1): NAME_WRITING, SELF_PORTRAIT.{fav_colour_context}

Score ONLY pair1_scores. Set pair2_scores to null, pair2_subtotal to 0, cross_pair_scores to null, cross_pair_subtotal to 0. Max score is 11.

Return JSON format with pair1_scores filled in and pair2_scores as null."""
        else:
            prompt_text = f"""Please score this PARTIAL assessment for {child_name}, age {age_years} years and {age_months_remainder} months.

Only 2 images (Pair 2): SUN_WRITING, SUN_DRAWING.{fav_colour_context}

Score ONLY pair2_scores. Set pair1_scores to null, pair1_subtotal to 0, cross_pair_scores to null, cross_pair_subtotal to 0. Max score is 11.

Return JSON format with pair2_scores filled in and pair1_scores as null."""
        
        user_content = [{"type": "text", "text": prompt_text}]
        
        if pair1_complete:
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['name_writing']}"}})
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['self_portrait']}"}})
        if pair2_complete:
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['sun_writing']}"}})
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['sun_drawing']}"}})
        
        # Sanitise favourite colour input
        def sanitise_colour_input(colour_input):
            if not colour_input:
                return "not specified"
            sanitised = re.sub(r'[^a-zA-Z\s-]', '', str(colour_input))
            sanitised = sanitised[:30].strip()
            return sanitised if sanitised else "not specified"
        
        safe_colour = sanitise_colour_input(favourite_colour_stated)
        logging.info(f"Sanitised colour: '{favourite_colour_stated}' -> '{safe_colour}'")
        
        personalized_rubric = TREIMAN_RUBRIC.replace("{favourite_colour_stated}", safe_colour)

        # =============================================================================
        # TRIPLE PARALLEL AI VERIFICATION
        # =============================================================================

        # =============================================================================
        # BLIND STRANGER TEST - No confirmation bias possible
        # =============================================================================
        
        blind_result = blind_stranger_test(
            client,
            deployment_name,
            name_image=images.get('name_writing'),
            sun_image=images.get('sun_writing')
        )
        
        logging.info(f"Blind test: established={blind_result.get('is_established')}, name={blind_result.get('name_word')}, sun={blind_result.get('sun_word')}")
        
        # If established writer, skip expensive triple verification
        if blind_result.get('is_established'):
            logging.info("Blind test detected CONVENTIONAL writer - returning early")
            
            stage_info = get_conventional_writing_feedback(
                child_name, 
                f"Writing shows recognisable letters: name={blind_result.get('name_word')}, sun={blind_result.get('sun_word')}"
            )
            verbal_interpretation = interpret_verbal_behaviour(questionnaire, 0, "ALREADY WRITING", child_name)
            
            final_response = {
                "success": True,
                "assessment_id": assessment_id,
                "partial_assessment": not (pair1_complete and pair2_complete),
                "pairs_completed": {"pair1": pair1_complete, "pair2": pair2_complete},
                "child": {
                    "name": child_name,
                    "age_months": child_age_months,
                    "age_display": f"{age_years} years, {age_months_remainder} months"
                },
                "email": recipient_email,
                "blind_test_result": blind_result,
                "visual_analysis": {
                    "writing_stage": "CONVENTIONAL",
                    "writing_stage_reasoning": f"Writing shows recognisable letters the writing without knowing child's name",
                    "observations": {
                        "name_writing": f"Clearly readable: \"{blind_result.get('name_word')}\" in {blind_result.get('name_colour', 'pencil/crayon')}" if blind_result.get('name_readable') else None,
                        "self_portrait": None,
                        "sun_writing": f"Clearly readable: \"sun\" in {blind_result.get('sun_colour', 'pencil/crayon')}" if blind_result.get('sun_readable') else None,
                        "sun_drawing": None
                    },
                    "favourite_colour_detected": blind_result.get('name_colour') or blind_result.get('sun_colour') or "none"
                },
                "scoring": {
                    "total_score": None,
                    "max_score": None,
                    "percentage": None,
                    "note": "Scores not applicable - child is already writing real words"
                },
                "interpretation": {
                    "stage": "ALREADY WRITING",
                    "stage_description": stage_info["description"],
                    "stage_detail": stage_info["detail"],
                    "writing_stage": "CONVENTIONAL",
                    "writing_stage_reasoning": stage_info.get("writing_stage_reasoning", ""),
                    "is_conventional_writer": True,
                    "recommendation": stage_info.get("recommendation", "")
                },
                "verbal_behaviour": verbal_interpretation,
                "metadata": {
                    "model_used": deployment_name,
                    "rubric_version": "blind-test-v1",
                    "api_version": "7.7.0",
                    "assessment_method": "blind_stranger_test"
                }
            }
            
            # Generate PDF if requested
            pdf_bytes = None
            if generate_pdf and REPORTLAB_AVAILABLE and STORAGE_AVAILABLE:
                try:
                    pdf_bytes = generate_assessment_pdf(final_response)
                    pdf_url = upload_pdf_to_blob(assessment_id, pdf_bytes)
                    final_response["pdf_url"] = pdf_url
                except Exception as e:
                    logging.warning(f'PDF generation failed: {str(e)}')
            
            # Send email with PDF attachment
            if recipient_email and EMAIL_AVAILABLE:
                try:
                    if pdf_bytes is None and REPORTLAB_AVAILABLE:
                        pdf_bytes = generate_assessment_pdf(final_response)
                    email_result = send_assessment_email(recipient_email, child_name, assessment_id, pdf_bytes)
                    final_response["email_sent"] = True
                    final_response["email_status"] = email_result
                    logging.info(f'Email sent successfully to {recipient_email}')
                except Exception as e:
                    logging.error(f'Failed to send email: {str(e)}')
                    final_response["email_sent"] = False
                    final_response["email_error"] = str(e)
            
            # Save to database (after PDF and email so all fields are populated)
            final_response["is_test"] = is_test_session
            final_response["image_names"] = [k for k, v in images.items() if v]
            if COSMOS_AVAILABLE:
                try:
                    save_assessment_to_db(final_response)
                    final_response["saved_to_database"] = True
                    
                    # Save anonymised admin stats (permanent) and refund lookup (14 days)
                    save_admin_stat(final_response)
                    if recipient_email:
                        save_refund_lookup(assessment_id, recipient_email)
                        
                except Exception as e:
                    logging.error(f'DB save failed: {str(e)}')
                    final_response["saved_to_database"] = False
            
            return func.HttpResponse(json.dumps(final_response), mimetype="application/json", status_code=200)
            
        
        # If not established, continue with full triple verification below



        
        logging.info("Starting triple parallel AI verification...")
        
        assessments = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(call_ai_once, client, deployment_name, personalized_rubric, user_content)
                for _ in range(3)
            ]
            
            for future in as_completed(futures):
                result = future.result()
                if result:
                    assessments.append(result)
                    logging.info(f"Collected assessment {len(assessments)}/3")
        
        if len(assessments) < 2:
            logging.error(f"Triple verification failed: only {len(assessments)} successful responses out of 3")
            return func.HttpResponse(
                json.dumps({
                    "error": "AI assessment failed - insufficient responses",
                    "details": f"Only {len(assessments)} of 3 AI assessments completed successfully",
                    "suggestion": "Please try again. If the problem persists, contact support."
                }),
                mimetype="application/json",
                status_code=500
            )
        
        if len(assessments) == 3:
            logging.info("All 3 assessments successful - building consensus")
            visual_scores = compare_three_assessments(assessments)
        else:
            logging.info("Only 2 assessments successful - using best one")
            visual_scores = max(assessments, key=lambda a: len(str(a.get('observations', {}))))
        
        logging.info("Final consensus assessment selected")
       
        
        # Clean all internal bracketed notes from observations
        if visual_scores.get('observations'):
            for key in visual_scores['observations']:
                if visual_scores['observations'][key]:
                    # Remove [Verified...], [Multiple assessments...], and any other bracketed notes
                    cleaned = re.sub(r'\s*\[[^\]]*\]', '', str(visual_scores['observations'][key])).strip()
                    visual_scores['observations'][key] = cleaned
   
        pair1_subtotal = visual_scores.get('pair1_subtotal', 0) if pair1_complete else 0
   
        pair1_subtotal = visual_scores.get('pair1_subtotal', 0) if pair1_complete else 0
        pair2_subtotal = visual_scores.get('pair2_subtotal', 0) if pair2_complete else 0
        cross_pair_subtotal = visual_scores.get('cross_pair_subtotal', 0) if (pair1_complete and pair2_complete) else 0

        pair2_both_are_drawings = visual_scores.get('pair2_both_are_drawings', False)
        
        writing_stage = visual_scores.get('writing_stage', 'UNKNOWN').upper()
        writing_stage_reasoning = visual_scores.get('writing_stage_reasoning', '')

        pair1_max = 10 if pair1_complete else 0
        pair2_max = 0 if pair2_both_are_drawings else (11 if pair2_complete else 0)
        cross_max = 3 if (pair1_complete and pair2_complete and not pair2_both_are_drawings) else 0
        max_score = pair1_max + pair2_max + cross_max
        
        total_score = pair1_subtotal + (0 if pair2_both_are_drawings else pair2_subtotal) + cross_pair_subtotal                  
        
        
        is_conventional_writer = writing_stage == 'CONVENTIONAL'
        
        if is_conventional_writer:
            # Check if short name cap applies
            name_letter_count = blind_result.get('name_letter_count', 0)
            sun_readable = blind_result.get('sun_readable', False)
            
            if name_letter_count <= 3 and not sun_readable:
                # Short name without sun - cap at EMERGING → Strong Start
                logging.info(f"Capping CONVENTIONAL to STRONG START: short name ({name_letter_count} letters) without sun")
                stage_info = {
                    "stage": "STRONG START",
                    "description": f"{child_name} is forming recognisable letters",
                    "detail": f"{child_name} can write their name with clear, recognisable letters - this is wonderful progress!",
                    "short_name_capped": True
                }
            else:
                # Normal CONVENTIONAL → Already Writing
                stage_info = get_conventional_writing_feedback(child_name, writing_stage_reasoning)
                stage_info["short_name_capped"] = False
                logging.info(f'CONVENTIONAL writing detected for {child_name}')
        else:
            # Use the new floor logic function for all non-conventional writers
            stage_info = determine_stage_with_writing_stage(
                total_score, 
                max_score, 
                child_age_months, 
                writing_stage, 
                child_name,
                blind_result
            )
            
            logging.info(f'Stage determined: {stage_info["stage"]} (writing_stage={writing_stage})')
        
        verbal_interpretation = interpret_verbal_behaviour(questionnaire, total_score, stage_info["stage"], child_name)
        percentage = round((total_score / max_score) * 100, 1) if max_score > 0 else 0
        
        interpretation_data = {
            "stage": stage_info["stage"],
            "stage_description": stage_info["description"],
            "stage_detail": stage_info["detail"],
            "writing_stage": writing_stage,
            "writing_stage_reasoning": writing_stage_reasoning,
            "short_name_capped": stage_info.get("short_name_capped", False)
        }
        
        if stage_info["stage"] == "ALREADY WRITING":
            interpretation_data["is_conventional_writer"] = True
            interpretation_data["recommendation"] = stage_info.get("recommendation", "")
        
        # Add writing development notes
        if writing_stage == "EMERGING":
            interpretation_data["writing_development"] = f"{child_name} is forming real letters - this is wonderful progress!"
        elif writing_stage == "LETTER_LIKE":
            interpretation_data["writing_development"] = f"{child_name} is making letter-like shapes in their writing attempts."
    
        final_response = {
            "success": True,
            "assessment_id": assessment_id,
            "partial_assessment": not (pair1_complete and pair2_complete),
            "pairs_completed": {"pair1": pair1_complete, "pair2": pair2_complete},
            "child": {
                "name": child_name,
                "age_months": child_age_months,
                "age_display": f"{age_years} years, {age_months_remainder} months"
            },
            "email": recipient_email,
            "visual_analysis": visual_scores,
            "scoring": {
                "total_score": total_score,
                "max_score": max_score,
                "percentage": percentage,
                "pair1_subtotal": pair1_subtotal if pair1_complete else None,
                "pair1_max": pair1_max if pair1_complete else None,
                "pair2_subtotal": pair2_subtotal if pair2_complete else None,
                "pair2_max": pair2_max if pair2_complete else None,
                "pair2_both_are_drawings": pair2_both_are_drawings if pair2_complete else False,
                "cross_pair_subtotal": cross_pair_subtotal if (pair1_complete and pair2_complete and not pair2_both_are_drawings) else None,
                "cross_pair_max": cross_max if (pair1_complete and pair2_complete and not pair2_both_are_drawings) else None
            },
            "interpretation": interpretation_data,
            "verbal_behaviour": verbal_interpretation,
            "metadata": {"model_used": deployment_name, "rubric_version": "Treiman-2011-24pt-v1", "api_version": "7.7.0"}
        }
        
        if pair1_complete and pair2_complete:
            final_response["pair_comparison"] = {
                "pair1_percentage": round((pair1_subtotal / 10) * 100, 1),
                "pair2_percentage": round((pair2_subtotal / 11) * 100, 1),
                "interpretation": get_pair_comparison_interpretation(visual_scores)
            }
        
        
        # Generate PDF if requested
        pdf_bytes = None
        if generate_pdf and REPORTLAB_AVAILABLE and STORAGE_AVAILABLE:
            try:
                pdf_bytes = generate_assessment_pdf(final_response)
                pdf_url = upload_pdf_to_blob(assessment_id, pdf_bytes)
                final_response["pdf_url"] = pdf_url
            except Exception as e:
                logging.warning(f'PDF generation failed: {str(e)}')
        
        # Send email with PDF attachment
        if recipient_email and EMAIL_AVAILABLE:
            try:
                if pdf_bytes is None and REPORTLAB_AVAILABLE:
                    pdf_bytes = generate_assessment_pdf(final_response)
                email_result = send_assessment_email(recipient_email, child_name, assessment_id, pdf_bytes)
                final_response["email_sent"] = True
                final_response["email_status"] = email_result
                logging.info(f'Email sent successfully to {recipient_email}')
            except Exception as e:
                logging.error(f'Failed to send email: {str(e)}')
                final_response["email_sent"] = False
                final_response["email_error"] = str(e)
        
        # Save to database (after PDF and email so all fields are populated)
        final_response["is_test"] = is_test_session
        final_response["image_names"] = [k for k, v in images.items() if v]
        if COSMOS_AVAILABLE:
            try:
                save_assessment_to_db(final_response)
                final_response["saved_to_database"] = True
                
                # Save anonymised admin stats (permanent) and refund lookup (14 days)
                save_admin_stat(final_response)
                if recipient_email:
                    save_refund_lookup(assessment_id, recipient_email)
                    
            except Exception as e:
                logging.error(f'DB save failed: {str(e)}')
                final_response["saved_to_database"] = False
        
        return func.HttpResponse(json.dumps(final_response), mimetype="application/json", status_code=200)
                
    except Exception as e:
        logging.error(f'Error scoring assessment: {str(e)}')
        return func.HttpResponse(json.dumps({"error": "Failed to score assessment", "details": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# ADMIN DASHBOARD - Accessed via /api/score_assessment?mode=admin&action=xxx
# =============================================================================

def _handle_admin(req):
    """Route admin actions. Called from score_assessment when mode=admin (GET or POST)."""
    if not verify_admin_password(req):
        return func.HttpResponse(
            json.dumps({"error": "Unauthorised"}),
            mimetype="application/json",
            status_code=401
        )
    
    action = req.params.get("action", "")
    
    try:
        if action == "stats":
            return _admin_stats(req)
        elif action == "refund_lookup":
            return _admin_refund_lookup(req)
        elif action == "mark_refunded":
            return _admin_mark_refunded(req)
        elif action == "purchase_stats":
            return _admin_purchase_stats(req)
        elif action == "create_test_session":
            return _admin_create_test_session(req)
        elif action == "customer_search":
            return _admin_customer_search(req)
        elif action == "get_image":
            return _admin_get_image(req)
        elif action == "get_report":
            return _admin_get_report(req)
        else:
            return func.HttpResponse(
                json.dumps({"error": f"Unknown action: {action}"}),
                mimetype="application/json",
                status_code=400
            )
    except Exception as e:
        logging.error(f"Admin error ({action}): {str(e)}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500
        )


def _admin_stats(req):
    container = get_admin_stats_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "AdminStats container not available"}), mimetype="application/json", status_code=503)
    
    product = req.params.get("product", "starter")
    days = int(req.params.get("days", "90"))
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = "SELECT * FROM c WHERE c.product = @product AND c.created_at >= @cutoff ORDER BY c.created_at DESC"
    params = [{"name": "@product", "value": product}, {"name": "@cutoff", "value": cutoff_date}]
    items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
    
    total = len(items)
    completed = len([i for i in items if i.get("status") == "completed"])
    refunded = len([i for i in items if i.get("refunded")])
    tests = len([i for i in items if i.get("is_test")])
    real_customers = total - tests
    
    age_bands = {}
    for item in items:
        band = item.get("age_band", "unknown")
        age_bands[band] = age_bands.get(band, 0) + 1
    
    stages = {}
    for item in items:
        stage = item.get("stage", "UNKNOWN")
        stages[stage] = stages.get(stage, 0) + 1
    
    by_date = {}
    for item in items:
        date_str = item.get("created_at", "")[:10]
        if date_str:
            by_date[date_str] = by_date.get(date_str, 0) + 1
    
    partial = len([i for i in items if i.get("partial_assessment")])
    full = total - partial
    
    scores = [i.get("score_percentage", 0) for i in items if i.get("score_percentage")]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    
    return func.HttpResponse(json.dumps({
        "period_days": days,
        "product": product,
        "summary": {
            "total_assessments": total,
            "real_customers": real_customers,
            "test_assessments": tests,
            "completed": completed,
            "refunded": refunded,
            "revenue_estimate": (real_customers - refunded) * 20
        },
        "age_bands": age_bands,
        "stages": stages,
        "by_date": dict(sorted(by_date.items())),
        "completion": {"full_assessments": full, "partial_assessments": partial},
        "average_score_percentage": avg_score,
        "recent": [
            {
                "assessment_id": i["assessment_id"],
                "created_at": i.get("created_at"),
                "age_band": i.get("age_band"),
                "stage": i.get("stage"),
                "score_percentage": i.get("score_percentage"),
                "status": i.get("status"),
                "is_test": i.get("is_test", False),
                "refunded": i.get("refunded", False)
            }
            for i in items[:50]
        ]
    }), mimetype="application/json", status_code=200)


def _admin_refund_lookup(req):
    container = get_refund_lookup_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "RefundLookup container not available"}), mimetype="application/json", status_code=503)
    
    days = int(req.params.get("days", "7"))
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = "SELECT * FROM c WHERE c.created_at >= @cutoff ORDER BY c.created_at DESC"
    params = [{"name": "@cutoff", "value": cutoff_date}]
    items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
    
    results = []
    for item in items:
        email = item.get("email", "")
        masked = email[0] + "****@" + email.split("@")[-1] if "@" in email else "****"
        results.append({
            "assessment_id": item["assessment_id"],
            "email_masked": masked,
            "email_full": email,
            "created_at": item.get("created_at"),
            "refunded": item.get("refunded", False)
        })
    
    return func.HttpResponse(json.dumps({"customers": results, "count": len(results), "period_days": days}), mimetype="application/json", status_code=200)


def _admin_mark_refunded(req):
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse(json.dumps({"error": "Invalid JSON"}), mimetype="application/json", status_code=400)
    
    assessment_id = req_body.get("assessment_id")
    if not assessment_id:
        return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
    
    updated = []
    try:
        stats_container = get_admin_stats_container()
        if stats_container:
            item = stats_container.read_item(item=assessment_id, partition_key=assessment_id)
            item["refunded"] = True
            item["refunded_at"] = datetime.utcnow().isoformat()
            stats_container.upsert_item(body=item)
            updated.append("AdminStats")
    except Exception as e:
        logging.warning(f"Could not update AdminStats: {str(e)}")
    
    try:
        refund_container = get_refund_lookup_container()
        if refund_container:
            item = refund_container.read_item(item=assessment_id, partition_key=assessment_id)
            item["refunded"] = True
            item["refunded_at"] = datetime.utcnow().isoformat()
            refund_container.upsert_item(body=item)
            updated.append("RefundLookup")
    except Exception as e:
        logging.warning(f"Could not update RefundLookup: {str(e)}")
    
    return func.HttpResponse(json.dumps({"success": True, "assessment_id": assessment_id, "updated": updated}), mimetype="application/json", status_code=200)


def _admin_purchase_stats(req):
    container = get_sessions_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "Sessions container not available"}), mimetype="application/json", status_code=503)
    
    days = int(req.params.get("days", "30"))
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = "SELECT * FROM c WHERE c.created_at >= @cutoff ORDER BY c.created_at DESC"
    params = [{"name": "@cutoff", "value": cutoff_date}]
    items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
    
    total_purchased = len(items)
    used = len([i for i in items if i.get("used")])
    unused = total_purchased - used
    
    by_date = {}
    for item in items:
        date_str = item.get("created_at", "")[:10]
        if date_str:
            by_date[date_str] = by_date.get(date_str, 0) + 1
    
    return func.HttpResponse(json.dumps({
        "period_days": days,
        "total_purchased": total_purchased,
        "used_sessions": used,
        "unused_sessions": unused,
        "by_date": dict(sorted(by_date.items()))
    }), mimetype="application/json", status_code=200)


def _admin_create_test_session(req):
    container = get_sessions_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "Sessions container not available"}), mimetype="application/json", status_code=503)
    
    test_token = f"test_{uuid.uuid4().hex[:12]}"
    expiry_date = datetime.utcnow() + timedelta(days=1)
    
    session_data = {
        "id": test_token,
        "session_token": test_token,
        "created_at": datetime.utcnow().isoformat(),
        "expires_at": expiry_date.isoformat(),
        "used": False,
        "is_test": True,
        "ttl": 1 * 24 * 60 * 60
    }
    
    container.upsert_item(body=session_data)
    access_url = f"https://earlywriting.morehandwriting.co.uk/?session={test_token}"
    
    return func.HttpResponse(json.dumps({
        "success": True,
        "session_token": test_token,
        "access_url": access_url,
        "expires_in": "24 hours"
    }), mimetype="application/json", status_code=200)


def _admin_customer_search(req):
    """Search for customer assessments by email within the 7-day window."""
    email_query = req.params.get("email", "").strip().lower()
    if not email_query:
        return func.HttpResponse(json.dumps({"error": "email parameter required"}), mimetype="application/json", status_code=400)
    
    container = get_cosmos_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "Assessments container not available"}), mimetype="application/json", status_code=503)
    
    try:
        # Partial match: supports searching with part of email
        query = "SELECT * FROM c WHERE CONTAINS(LOWER(c.email), @email) ORDER BY c.created_at DESC"
        params = [{"name": "@email", "value": email_query}]
        items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
        
        # Check blob storage for images and reports for each result
        blob_service = get_blob_service_client()
        
        results = []
        for item in items:
            assessment_id = item.get("assessment_id", item.get("id", ""))
            scoring = item.get("scoring", {})
            interpretation = item.get("interpretation", {})
            
            # Check blob storage for actual images
            found_images = []
            if blob_service:
                try:
                    uploads_container = blob_service.get_container_client("uploads")
                    prefix = f"{assessment_id}/"
                    blobs = uploads_container.list_blobs(name_starts_with=prefix)
                    for blob in blobs:
                        img_name = blob.name.replace(prefix, "").replace(".png", "")
                        found_images.append(img_name)
                except Exception:
                    pass
            
            # Check if report exists in blob
            has_report = False
            if blob_service:
                try:
                    reports_container = blob_service.get_container_client("reports")
                    blob_client = reports_container.get_blob_client(f"{assessment_id}/report.pdf")
                    blob_client.get_blob_properties()
                    has_report = True
                except Exception:
                    pass
            
            results.append({
                "assessment_id": assessment_id,
                "email": item.get("email", ""),
                "created_at": item.get("created_at", ""),
                "child_age_months": item.get("child", {}).get("age_months", 0),
                "stage": interpretation.get("stage", "UNKNOWN"),
                "writing_stage": interpretation.get("writing_stage", ""),
                "score_percentage": scoring.get("percentage", 0),
                "total_score": scoring.get("total_score", 0),
                "max_score": scoring.get("max_score", 0),
                "has_report": has_report,
                "is_test": item.get("is_test", False),
                "image_names": found_images
            })
        
        return func.HttpResponse(json.dumps({
            "results": results,
            "count": len(results),
            "email_searched": email_query
        }), mimetype="application/json", status_code=200)
    
    except Exception as e:
        logging.error(f"Customer search error: {str(e)}")
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


def _admin_get_image(req):
    """Serve an uploaded image from blob storage."""
    assessment_id = req.params.get("assessment_id", "")
    image_name = req.params.get("image_name", "")
    if not assessment_id or not image_name:
        return func.HttpResponse(json.dumps({"error": "assessment_id and image_name required"}), mimetype="application/json", status_code=400)
    
    try:
        blob_service = get_blob_service_client()
        if not blob_service:
            return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
        container_client = blob_service.get_container_client("uploads")
        blob_name = f"{assessment_id}/{image_name}.png"
        blob_client = container_client.get_blob_client(blob_name)
        image_data = blob_client.download_blob().readall()
        return func.HttpResponse(image_data, mimetype="image/png", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": f"Image not found: {str(e)}"}), mimetype="application/json", status_code=404)


def _admin_get_report(req):
    """Serve a PDF report from blob storage."""
    assessment_id = req.params.get("assessment_id", "")
    if not assessment_id:
        return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
    
    try:
        pdf_data = get_pdf_from_blob(assessment_id)
        return func.HttpResponse(
            pdf_data,
            mimetype="application/pdf",
            status_code=200,
            headers={"Content-Disposition": f"inline; filename=report_{assessment_id[:8]}.pdf"}
        )
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": f"Report not found: {str(e)}"}), mimetype="application/json", status_code=404)import azure.functions as func
import logging
import json
import os
import uuid
import base64
from datetime import datetime, timedelta
from io import BytesIO

# Import OpenAI
try:
    from openai import AzureOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logging.warning("OpenAI package not available")

# Import Azure Storage
try:
    from azure.storage.blob import BlobServiceClient, ContentSettings
    STORAGE_AVAILABLE = True
except ImportError:
    STORAGE_AVAILABLE = False
    logging.warning("Azure Storage package not available")

# Import Cosmos DB
try:
    from azure.cosmos import CosmosClient, exceptions
    COSMOS_AVAILABLE = True
except ImportError:
    COSMOS_AVAILABLE = False
    logging.warning("Azure Cosmos package not available")

# Import ReportLab for PDF generation
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm, cm
    from reportlab.lib.colors import HexColor, black, white
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    logging.warning("ReportLab package not available")

# Import PIL for image handling
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("PIL package not available")

# Import Azure Communication Services for email
try:
    from azure.communication.email import EmailClient
    EMAIL_AVAILABLE = True
except ImportError:
    EMAIL_AVAILABLE = False
    logging.warning("Azure Communication Email package not available")

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# =============================================================================
# HELPER FUNCTIONS - Storage
# =============================================================================

def get_blob_service_client():
    conn_str = os.environ.get("STORAGE_CONNECTION_STRING")
    if not conn_str:
        return None
    return BlobServiceClient.from_connection_string(conn_str)


def upload_image_to_blob(assessment_id: str, image_name: str, image_data: bytes) -> str:
    blob_service = get_blob_service_client()
    if not blob_service:
        raise Exception("Storage connection string not configured")
    container_client = blob_service.get_container_client("uploads")
    blob_name = f"{assessment_id}/{image_name}.png"
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(image_data, overwrite=True)
    return blob_client.url


def upload_pdf_to_blob(assessment_id: str, pdf_data: bytes) -> str:
    blob_service = get_blob_service_client()
    if not blob_service:
        raise Exception("Storage connection string not configured")
    container_client = blob_service.get_container_client("reports")
    blob_name = f"{assessment_id}/report.pdf"
    blob_client = container_client.get_blob_client(blob_name)
    content_settings = ContentSettings(content_type='application/pdf')
    blob_client.upload_blob(pdf_data, overwrite=True, content_settings=content_settings)
    return blob_client.url


def get_pdf_from_blob(assessment_id: str) -> bytes:
    blob_service = get_blob_service_client()
    if not blob_service:
        raise Exception("Storage connection string not configured")
    container_client = blob_service.get_container_client("reports")
    blob_name = f"{assessment_id}/report.pdf"
    blob_client = container_client.get_blob_client(blob_name)
    return blob_client.download_blob().readall()


def get_logo_from_blob() -> bytes:
    blob_service = get_blob_service_client()
    if not blob_service:
        return None
    try:
        container_client = blob_service.get_container_client("uploads")
        blob_client = container_client.get_blob_client("logo.png")
        return blob_client.download_blob().readall()
    except Exception:
        return None


# =============================================================================
# HELPER FUNCTIONS - Cosmos DB
# =============================================================================

def get_cosmos_container():
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("Assessments")
    return container


def save_assessment_to_db(assessment_data: dict, ttl_days: int = 7) -> dict:
    container = get_cosmos_container()
    if not container:
        raise Exception("Cosmos DB connection string not configured")
    data_to_save = assessment_data.copy()
    if "id" not in data_to_save:
        data_to_save["id"] = data_to_save.get("assessment_id", str(uuid.uuid4()))
    data_to_save["assessment_id"] = data_to_save["id"]
    data_to_save["created_at"] = datetime.utcnow().isoformat()
    data_to_save["updated_at"] = datetime.utcnow().isoformat()
    data_to_save["ttl"] = ttl_days * 24 * 60 * 60
    # Privacy: replace child's name before storing (email kept for 7-day customer lookup)
    if "child" in data_to_save:
        data_to_save["child"]["name"] = "Child"
    result = container.upsert_item(body=data_to_save)
    return result


def get_assessment_from_db(assessment_id: str) -> dict:
    container = get_cosmos_container()
    if not container:
        raise Exception("Cosmos DB connection string not configured")
    try:
        item = container.read_item(item=assessment_id, partition_key=assessment_id)
        return item
    except exceptions.CosmosResourceNotFoundError:
        return None

def get_admin_stats_container():
    """Get the AdminStats container - stores anonymised records permanently."""
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("AdminStats")
    return container


def get_refund_lookup_container():
    """Get the RefundLookup container - stores email + assessment_id for 14 days."""
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("RefundLookup")
    return container


def get_age_band(age_months: int) -> str:
    """Convert age in months to anonymised age band."""
    if age_months < 30:
        return "2-2.5"
    elif age_months < 36:
        return "2.5-3"
    elif age_months < 42:
        return "3-3.5"
    else:
        return "3.5+"


def save_admin_stat(assessment_data: dict):
    """Save an anonymised record to AdminStats. No name, no email, no images."""
    try:
        container = get_admin_stats_container()
        if not container:
            logging.warning("AdminStats container not available")
            return
        
        assessment_id = assessment_data.get("assessment_id", str(uuid.uuid4()))
        age_months = assessment_data.get("child", {}).get("age_months", 0)
        scoring = assessment_data.get("scoring", {})
        interpretation = assessment_data.get("interpretation", {})
        
        stat_record = {
            "id": assessment_id,
            "assessment_id": assessment_id,
            "product": "starter",
            "created_at": datetime.utcnow().isoformat(),
            "age_band": get_age_band(age_months),
            "stage": interpretation.get("stage", "UNKNOWN"),
            "writing_stage": interpretation.get("writing_stage", "UNKNOWN"),
            "score_percentage": scoring.get("percentage", 0),
            "total_score": scoring.get("total_score", 0),
            "max_score": scoring.get("max_score", 0),
            "pairs_completed": assessment_data.get("pairs_completed", {}),
            "partial_assessment": assessment_data.get("partial_assessment", False),
            "email_sent": assessment_data.get("email_sent", False),
            "status": "completed",
            "is_test": assessment_data.get("is_test", False),
            "refunded": False
        }
        
        container.upsert_item(body=stat_record)
        logging.info(f"Admin stat saved for {assessment_id}")
    except Exception as e:
        logging.error(f"Failed to save admin stat: {str(e)}")


def save_refund_lookup(assessment_id: str, email: str):
    """Save email + assessment_id for refund lookup. 14-day TTL."""
    try:
        container = get_refund_lookup_container()
        if not container:
            logging.warning("RefundLookup container not available")
            return
        
        record = {
            "id": assessment_id,
            "assessment_id": assessment_id,
            "email": email,
            "created_at": datetime.utcnow().isoformat(),
            "refunded": False,
            "ttl": 14 * 24 * 60 * 60
        }
        
        container.upsert_item(body=record)
        logging.info(f"Refund lookup saved for {assessment_id}")
    except Exception as e:
        logging.error(f"Failed to save refund lookup: {str(e)}")


def verify_admin_password(req: func.HttpRequest) -> bool:
    """Check the admin password from request header or query param."""
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_password:
        logging.error("ADMIN_PASSWORD not set in app settings")
        return False
    
    provided = req.headers.get("X-Admin-Password", "")
    if not provided:
        provided = req.params.get("admin_password", "")
    
    return provided == admin_password


# =============================================================================
# EMAIL FUNCTIONS
# =============================================================================

def get_email_client():
    conn_str = os.environ.get("EMAIL_CONNECTION_STRING")
    if not conn_str:
        return None
    return EmailClient.from_connection_string(conn_str)


def send_assessment_email(recipient_email: str, child_name: str, assessment_id: str, pdf_bytes: bytes = None) -> dict:
    email_client = get_email_client()
    if not email_client:
        raise Exception("Email connection string not configured")
    
    subject = f"Early Writing Assessment Report for {child_name}"
    
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #1B75BC;">Early Writing Assessment Report</h1>
            <p>Dear Parent/Guardian,</p>
            <p>Thank you for completing the Early Writing Assessment for <strong>{child_name}</strong>.</p>
            <p>Please find attached the detailed assessment report.</p>
            <p>Best wishes,<br/><strong>The More Handwriting Team</strong></p>
            <hr style="border: none; border-top: 1px solid #ccc; margin: 30px 0;" />
            <p style="font-size: 12px; color: #666;">
                © More Handwriting | <a href="https://morehandwriting.co.uk">morehandwriting.co.uk</a>
            </p>
        </div>
    </body>
    </html>
    """
    
    message = {
        "senderAddress": "DoNotReply@morehandwriting.co.uk",
        "recipients": {"to": [{"address": recipient_email}]},
        "content": {"subject": subject, "html": html_content}
    }
    
    if pdf_bytes:
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        message["attachments"] = [{
            "name": f"Assessment_Report_{child_name}.pdf",
            "contentType": "application/pdf",
            "contentInBase64": pdf_base64
        }]
    
    poller = email_client.begin_send(message)
    result = poller.result()
    return {"message_id": result.get("id", ""), "status": "sent"}


# =============================================================================
# PDF GENERATION
# =============================================================================

def generate_assessment_pdf(assessment_data: dict) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise Exception("ReportLab not available")
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    
    # Calculate available width for content
    page_width = A4[0]  # 595 points
    available_width = page_width - 4*cm  # ~480 points
    
    # Colours
    primary_blue = HexColor('#1B75BC')
    light_blue = HexColor('#E8F4FC')
    light_grey = HexColor('#F5F5F5')
    dark_grey = HexColor('#666666')
    medium_grey = HexColor('#888888')
    success_green = HexColor('#28A745')
    light_green = HexColor('#E8F8E8')
    warning_amber = HexColor('#F5A623')
    light_amber = HexColor('#FFF9E6')
    info_blue = HexColor('#17A2B8')
    light_info = HexColor('#E3F6F9')
    highlight_bg = HexColor('#FFFBF0')
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=22, textColor=primary_blue, spaceAfter=6, alignment=TA_CENTER)
    subtitle_style = ParagraphStyle('CustomSubtitle', parent=styles['Normal'], fontSize=13, textColor=dark_grey, spaceAfter=4, alignment=TA_CENTER)
    meta_style = ParagraphStyle('MetaStyle', parent=styles['Normal'], fontSize=9, textColor=medium_grey, spaceAfter=3, alignment=TA_CENTER)
    heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading2'], fontSize=14, textColor=primary_blue, spaceBefore=15, spaceAfter=8)
    subheading_style = ParagraphStyle('CustomSubheading', parent=styles['Heading3'], fontSize=11, textColor=dark_grey, spaceBefore=10, spaceAfter=4, fontName='Helvetica-Bold')
    body_style = ParagraphStyle('CustomBody', parent=styles['Normal'], fontSize=10, textColor=black, spaceAfter=6, leading=14)
    small_style = ParagraphStyle('CustomSmall', parent=styles['Normal'], fontSize=8, textColor=medium_grey, spaceAfter=4)
    bullet_style = ParagraphStyle('BulletStyle', parent=styles['Normal'], fontSize=10, textColor=dark_grey, spaceAfter=4, leftIndent=15, leading=14)
    observation_style = ParagraphStyle('ObsStyle', parent=styles['Normal'], fontSize=9, textColor=dark_grey, spaceAfter=8, leading=13)
    activity_title_style = ParagraphStyle('ActivityTitle', parent=styles['Normal'], fontSize=11, textColor=primary_blue, spaceBefore=10, spaceAfter=3, fontName='Helvetica-Bold')
    activity_body_style = ParagraphStyle('ActivityBody', parent=styles['Normal'], fontSize=9, textColor=dark_grey, spaceAfter=8, leading=13, leftIndent=0)
    
    story = []
    
    # Logo
    logo_data = get_logo_from_blob()
    if logo_data and PIL_AVAILABLE:
        try:
            logo_image = PILImage.open(BytesIO(logo_data))
            logo_width = 130
            aspect = logo_image.height / logo_image.width
            logo_height = logo_width * aspect
            logo_buffer = BytesIO()
            logo_image.save(logo_buffer, format='PNG')
            logo_buffer.seek(0)
            img = Image(logo_buffer, width=logo_width, height=logo_height)
            story.append(img)
            story.append(Spacer(1, 8))
        except Exception as e:
            logging.warning(f"Could not add logo: {e}")
    
    # Header
    story.append(Paragraph("Early Writing Starter Report", title_style))
    
    child = assessment_data.get('child', {})
    child_name = child.get('name', 'Unknown')
    age_display = child.get('age_display', '')
    child_age_months = child.get('age_months', 36)
    
    story.append(Paragraph(f"For {child_name}", subtitle_style))
    story.append(Paragraph(f"Age: {age_display}  •  {datetime.now().strftime('%d %B %Y')}", meta_style))
    
    if assessment_data.get('partial_assessment'):
        story.append(Spacer(1, 6))
        story.append(Paragraph("<i>Based on one pair of samples</i>", meta_style))
    
    # Disclaimer
    story.append(Spacer(1, 20))
    disclaimer_style = ParagraphStyle('Disclaimer', parent=styles['Normal'], fontSize=8, textColor=medium_grey, alignment=TA_CENTER, leading=10)
    story.append(Paragraph("This assessment provides guidance based on research, not a definitive evaluation. If you have concerns about your child's development, consult a qualified professional.", disclaimer_style))
    
    story.append(Spacer(1, 15))
    
    # Get interpretation data
    interpretation = assessment_data.get('interpretation', {})
    stage = interpretation.get('stage', 'Unknown')
    
    # Stage styling
    if stage == "ALREADY WRITING":
        stage_color = success_green
        stage_bg = light_green
        stage_display = "Already Writing!"
    elif stage == "STRONG START":
        stage_color = success_green
        stage_bg = light_green
        stage_display = "Strong Start"
    elif stage == "BEGINNING EXPLORER":
        stage_color = warning_amber
        stage_bg = light_amber
        stage_display = "Beginning Explorer"
    else:
        stage_color = info_blue
        stage_bg = light_info
        stage_display = "Early Days"
    
    # Stage box - NO bullet point, full width to match text
    stage_title_style = ParagraphStyle('StageTitle', fontSize=18, textColor=stage_color, alignment=TA_CENTER, fontName='Helvetica-Bold')
    
    stage_content = [
        [Paragraph(stage_display, stage_title_style)],
    ]
    
    # Use available_width to match text alignment
    stage_table = Table(stage_content, colWidths=[available_width])
    stage_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), stage_bg),
        ('TOPPADDING', (0, 0), (-1, -1), 15),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 15),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(stage_table)
    
    story.append(Spacer(1, 12))

    # Short name explanation box (only shows if assessment was capped due to short name)
    if interpretation.get('short_name_capped', False):
        short_name_note_style = ParagraphStyle('ShortNameNote', fontSize=9, textColor=HexColor('#1B75BC'), leading=12)
        short_name_text = f"Note: Because {child_name}'s name has only 2-3 letters, we recommend completing the 'sun' writing task to fully assess their letter knowledge."
        short_name_content = [[Paragraph(short_name_text, short_name_note_style)]]
        short_name_table = Table(short_name_content, colWidths=[available_width])
        short_name_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#E8F4FD')),
            ('BOX', (0, 0), (-1, -1), 1, HexColor('#1B75BC')),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(short_name_table)
        story.append(Spacer(1, 12))
    
    # Get visual analysis data
    visual_analysis = assessment_data.get('visual_analysis', {})
    observations = visual_analysis.get('observations', {})
    pairs_completed = assessment_data.get('pairs_completed', {'pair1': True, 'pair2': True})
    
    # Development Journey Visual
    scoring = assessment_data.get('scoring', {})
    total_score = scoring.get('total_score', 0)
    max_score = scoring.get('max_score', 25)
    percentage = scoring.get('percentage', 0)
    
    journey_title_style = ParagraphStyle('JourneyTitle', fontSize=9, textColor=medium_grey, alignment=TA_CENTER, spaceAfter=8)
    story.append(Paragraph("Development Journey", journey_title_style))
    
    # Calculate column widths to match available_width
    col_width = available_width / 3
    
    # Journey stages labels
    # For ALREADY WRITING, highlight Strong Start since they have moved beyond it
    is_beyond_assessment = stage == "ALREADY WRITING"
    journey_labels = Table(
        [[
            Paragraph("Early Days", ParagraphStyle('JL', fontSize=8, textColor=info_blue if stage == "EARLY DAYS" else medium_grey, alignment=TA_LEFT)),
            Paragraph("Beginning Explorer", ParagraphStyle('JL', fontSize=8, textColor=warning_amber if stage == "BEGINNING EXPLORER" else medium_grey, alignment=TA_CENTER)),
            Paragraph("Strong Start", ParagraphStyle('JL', fontSize=8, textColor=success_green if (stage == "STRONG START" or is_beyond_assessment) else medium_grey, alignment=TA_RIGHT)),
        ]],
        colWidths=[col_width, col_width, col_width]
    )
    journey_labels.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(journey_labels)
    
    # Progress bar
    bar_height = 8
    bar_data = [['', '', '']]
    bar_table = Table(bar_data, colWidths=[col_width, col_width, col_width], rowHeights=[bar_height])
    
    if stage == "ALREADY WRITING" or stage == "STRONG START":
        # Both show all bars filled - ALREADY WRITING has moved beyond this scale
        bar_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), info_blue),
            ('BACKGROUND', (1, 0), (1, 0), warning_amber),
            ('BACKGROUND', (2, 0), (2, 0), success_green),
        ]))
    elif stage == "BEGINNING EXPLORER":
        bar_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), info_blue),
            ('BACKGROUND', (1, 0), (1, 0), warning_amber),
            ('BACKGROUND', (2, 0), (2, 0), light_grey),
        ]))
    else:  # EARLY DAYS
        bar_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), info_blue),
            ('BACKGROUND', (1, 0), (1, 0), light_grey),
            ('BACKGROUND', (2, 0), (2, 0), light_grey),
        ]))
    
    story.append(bar_table)
    story.append(Spacer(1, 15))
    
    # Comprehensive summary - detailed and age-contextualised
    favourite_colour_stated = assessment_data.get('questionnaire', {}).get('favourite_colour', '')
    comprehensive_summary = generate_comprehensive_summary(visual_analysis, child_name, stage, child_age_months, favourite_colour_stated)
    summary_style = ParagraphStyle('ComprehensiveSummary', fontSize=10, textColor=dark_grey, alignment=TA_LEFT, leading=15, spaceAfter=15)
    story.append(Paragraph(comprehensive_summary, summary_style))
    
    story.append(Spacer(1, 10))
    
    # What We Observed
    story.append(Paragraph("What We Observed", heading_style))
    
    obs_style = ParagraphStyle('ObsItem', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=6)
    
    if observations.get('name_writing'):
        story.append(Paragraph(f"<b>Name Writing:</b> {observations.get('name_writing')}", obs_style))
    if observations.get('self_portrait'):
        story.append(Paragraph(f"<b>Self Portrait:</b> {observations.get('self_portrait')}", obs_style))
    if observations.get('sun_writing'):
        story.append(Paragraph(f"<b>Sun Writing:</b> {observations.get('sun_writing')}", obs_style))
    if observations.get('sun_drawing'):
        story.append(Paragraph(f"<b>Sun Drawing:</b> {observations.get('sun_drawing')}", obs_style))
    
    story.append(Spacer(1, 15))

  # Metacognitive awareness insight - ONLY for children who CAN write but chose to draw
    pair2_both_drawings = assessment_data.get('scoring', {}).get('pair2_both_are_drawings', False)
    writing_stage = assessment_data.get('visual_analysis', {}).get('writing_stage', '').upper()
    
    # Only show this note if child demonstrates writing ability (CONVENTIONAL or EMERGING)
    # but drew for sun tasks - this shows metacognitive awareness that they don't know how to spell "sun"
    # Do NOT show for SCRIBBLES/LETTER_LIKE - those children aren't making a metacognitive choice
    child_can_write = writing_stage in ['CONVENTIONAL', 'EMERGING']
    
    if pair2_both_drawings and child_can_write:
        story.append(Spacer(1, 10))
        metacog_style = ParagraphStyle('MetacogStyle', parent=styles['Normal'], fontSize=10, textColor=HexColor('#5D4E37'), leading=14)
        
        metacog_text = (
            f"Note: Both sun samples were drawings, but this does not mean {child_name} lacks understanding. "
            f"Research shows that children write with more skill when they know how to spell a word (like their name) "
            f"than when they do not. When children do not yet know how to write a word (like 'sun'), drawing is a "
            f"natural response. This is age-appropriate at {age_display}."
        )
        
        metacog_content = [[Paragraph(metacog_text, metacog_style)]]
        metacog_table = Table(metacog_content, colWidths=[available_width - 20])
        metacog_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#FFF9E6')),
            ('BOX', (0, 0), (-1, -1), 1, warning_amber),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        story.append(metacog_table)
        story.append(Spacer(1, 15))
    
    story.append(Spacer(1, 15))
    
    # What child showed us
    story.append(Paragraph(f"What {child_name} Showed Us", heading_style))
           
    strengths = build_pdf_strengths(visual_analysis, child_name, stage, child_age_months)
    strength_style = ParagraphStyle('StrengthItem', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=4)
    for strength in strengths:
        story.append(Paragraph(f"✓ {strength}", strength_style))
    
    # Parent observations
    verbal = assessment_data.get('verbal_behaviour', {})
    interpretations = verbal.get('interpretations', [])
    if interpretations:
        story.append(Spacer(1, 15))
        story.append(Paragraph("Your Observations", heading_style))
        obs_bullet_style = ParagraphStyle('ObsBullet', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=4)
        for interp in interpretations:
            story.append(Paragraph(f"• {interp}", obs_bullet_style))
    
    # Activities
    story.append(Spacer(1, 15))
# SAFETY DISCLAIMER
    disclaimer_style = ParagraphStyle(
        'DisclaimerStyle',
        parent=styles['Normal'],
        fontSize=9,
        textColor=HexColor('#4a4a4a'),
        leading=11,
        spaceAfter=6,
        leftIndent=10,
        rightIndent=10
    )
    
    disclaimer_heading_style = ParagraphStyle(
        'DisclaimerHeading',
        parent=styles['Normal'],
        fontSize=10,
        textColor=HexColor('#2c2c2c'),
        fontName='Helvetica-Bold',
        spaceAfter=4,
        leftIndent=10,
        rightIndent=10
    )
    
    # Create disclaimer box content
    disclaimer_heading = Paragraph("IMPORTANT SAFETY INFORMATION", disclaimer_heading_style)
    disclaimer_text = Paragraph(
        "All activities recommended in this report require active adult supervision. These activities are designed for children aged 24-42 months based on typical development, but every child is unique. Parents and caregivers must use their own judgment to determine whether each activity is appropriate for their child's individual abilities, interests and safety needs.<br/><br/>"
        "This report provides educational guidance only and is not a substitute for medical, therapeutic or professional advice. More Handwriting is not liable for any injury, accident or adverse outcome resulting from activities undertaken based on this report. Parents and caregivers assume full responsibility for their child's safety during all activities.",
        disclaimer_style
    )
    
    # Create table for grey box effect
    disclaimer_table = Table(
        [[disclaimer_heading], [disclaimer_text]],
        colWidths=[480]
    )
    disclaimer_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), HexColor('#f5f5f5')),
        ('BOX', (0, 0), (-1, -1), 1, HexColor('#d0d0d0')),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    story.append(KeepTogether([disclaimer_table, Spacer(1, 15)]))
    # END DISCLAIMER
    
    story.append(Paragraph("Recommended Activities", heading_style))
    
    # Get email for sibling detection
    recipient_email = assessment_data.get('email', '')
    activities = get_recommended_activities(stage, total_score, child_name, child_age_months, recipient_email)
 
   
    for activity in activities:
        story.append(Paragraph(activity['title'], activity_title_style))
        story.append(Paragraph(activity['description'], activity_body_style))
        if activity.get('research_note'):
            research_note_style = ParagraphStyle('ResearchNote', fontSize=8, textColor=medium_grey, leading=11, spaceAfter=8, leftIndent=10)
            story.append(Paragraph(f"<i>{activity['research_note']}</i>", research_note_style))
    
    # =========================================================================
    # NEW SECTION: What to Look For Next
    # =========================================================================
    story.append(Spacer(1, 20))
    story.append(Paragraph("What to Look For Next", heading_style))
    
    milestones = get_developmental_milestones(stage, child_age_months, child_name)
    milestone_style = ParagraphStyle('MilestoneItem', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=6, leftIndent=15)
    milestone_intro = ParagraphStyle('MilestoneIntro', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=8)
    
    story.append(Paragraph(milestones['intro'], milestone_intro))
    for milestone in milestones['signs']:
        story.append(Paragraph(f"• {milestone}", milestone_style))
    
    if milestones.get('note'):
        note_style = ParagraphStyle('MilestoneNote', fontSize=9, textColor=medium_grey, leading=13, spaceAfter=6, spaceBefore=8)
        story.append(Paragraph(f"<i>{milestones['note']}</i>", note_style))
    
    # =========================================================================
    # NEW SECTION: Understanding This Assessment
    # =========================================================================
    story.append(Spacer(1, 20))
    story.append(Paragraph("Understanding This Assessment", heading_style))
    
    understanding_style = ParagraphStyle('UnderstandingText', fontSize=9, textColor=dark_grey, leading=13, spaceAfter=8)
    
    story.append(Paragraph(
        "This assessment is based on peer-reviewed research into how young children develop an understanding that writing and drawing are different. "
        "Researchers found that children as young as 2 years and 8 months begin to show this understanding in subtle but measurable ways.",
        understanding_style
    ))
    
    story.append(Paragraph(
        "When children understand the difference, their writing tends to be: smaller than their drawings, darker in colour, "
        "more angular (with straighter lines) and sparser (with fewer marks). Their drawings tend to be: larger, more colourful, "
        "more curved and denser. This assessment measures these differences across four samples.",
        understanding_style
    ))
    
    story.append(Paragraph(
        f"<b>What the stages mean:</b>", understanding_style
    ))
    
    stages_explanation = [
        "<b>Early Days:</b> Writing and drawing look similar. The child is still exploring mark-making, which is a normal and necessary stage before differentiation develops.",
        "<b>Beginning Explorer:</b> Some differences are emerging between writing and drawing. The child is starting to understand these are different activities.",
        "<b>Strong Start:</b> Clear differences between writing and drawing. The child shows good understanding that these serve different purposes."
    ]
    
    for stage_exp in stages_explanation:
        story.append(Paragraph(f"• {stage_exp}", milestone_style))
    
    # =========================================================================
    # NEW SECTION: Try Again in 3 Months
    # =========================================================================
    story.append(Spacer(1, 20))
    
    # Create a highlighted box for the reassessment prompt
    # Skip for conventional writers - they do not need to reassess
    if stage != "ALREADY WRITING":
        reassess_elements = []
        
        reassess_title = ParagraphStyle('ReassessTitle', fontSize=11, textColor=primary_blue, alignment=TA_LEFT, fontName='Helvetica-Bold', spaceAfter=6)
        reassess_body = ParagraphStyle('ReassessBody', fontSize=9, textColor=dark_grey, leading=13)
        
        reassess_elements.append([Paragraph("Track Progress: Try Again in 3 Months", reassess_title)])
        
        if stage == "STRONG START":
            reassess_text = (
                f"At {child_name}'s current stage, you might like to repeat this activity in 3-6 months to see how their "
                f"writing and drawing continue to develop. Look for increasingly letter-like shapes in their writing attempts, "
                f"and more detailed, representational drawings."
            )
        elif stage == "BEGINNING EXPLORER":
            reassess_text = (
                f"Children at this stage often make rapid progress. Try this activity again in 3 months to see how {child_name}'s "
                f"understanding has developed. With the activities suggested above, you may see clearer differences between "
                f"their writing and drawing next time."
            )
        else:
            reassess_text = (
                f"Repeating this activity in 3 months will show you how {child_name}'s mark-making is developing. "
                f"With regular exposure to books and print and the activities suggested above, you may see the first signs "
                f"of differentiation emerging. Remember that children develop at different rates and all progress is valuable."
            )
        
        reassess_elements.append([Paragraph(reassess_text, reassess_body)])
        reassess_elements.append([Paragraph(f"<b>Suggested reassessment date:</b> {(datetime.now() + timedelta(days=90)).strftime('%B %Y')}", reassess_body)])
        
        reassess_box = Table(reassess_elements, colWidths=[available_width - 30])
        reassess_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), highlight_bg),
            ('BOX', (0, 0), (-1, -1), 1, warning_amber),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 12),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ]))
        story.append(reassess_box)
    else:
        # For conventional writers, show celebratory message instead
        congrats_elements = []
        
        congrats_title = ParagraphStyle('CongratsTitle', fontSize=11, textColor=success_green, alignment=TA_LEFT, fontName='Helvetica-Bold', spaceAfter=6)
        congrats_body = ParagraphStyle('CongratsBody', fontSize=9, textColor=dark_grey, leading=13)
        
        congrats_elements.append([Paragraph("Already Writing!", congrats_title)])
        congrats_text = (
            f"{child_name} is already writing real letters - that is wonderful! This particular assessment is designed for "
            f"children who are still learning that writing and drawing are different, so the scoring system is not really "
            f"relevant for where {child_name} is now. Instead, enjoy building on their strong foundation with the "
            f"activities suggested above."
        )
        congrats_elements.append([Paragraph(congrats_text, congrats_body)])
        
        congrats_box = Table(congrats_elements, colWidths=[available_width - 30])
        congrats_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), light_green),
            ('BOX', (0, 0), (-1, -1), 1, success_green),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 12),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ]))
        story.append(congrats_box)
    
    # Detailed Breakdown section - skip for Already Writing (scores not meaningful)
    if stage != "ALREADY WRITING":
        story.append(Spacer(1, 25))
        story.append(Paragraph("—  —  —  —  —", ParagraphStyle('Divider', fontSize=10, textColor=light_grey, alignment=TA_CENTER, spaceAfter=15)))
        story.append(Spacer(1, 10))
        
        breakdown_elements = []
        
        breakdown_title = ParagraphStyle('BreakdownTitle', fontSize=12, textColor=primary_blue, alignment=TA_CENTER, fontName='Helvetica-Bold', spaceAfter=4)
        breakdown_elements.append([Paragraph("Detailed Breakdown", breakdown_title)])
        
        breakdown_sub = ParagraphStyle('BreakdownSub', fontSize=8, textColor=medium_grey, alignment=TA_CENTER, spaceAfter=12)
    
        breakdown_elements.append([Paragraph("For parents who would like more detail. These scores help us understand where your child is - not a test or judgement.", breakdown_sub)])
        
        if stage == "STRONG START":
            score_context = "Clear differentiation between writing and drawing."
        elif stage == "BEGINNING EXPLORER":
            score_context = "Emerging awareness that writing and drawing are different."
        else:
            score_context = "Still exploring mark-making - a normal starting point."
        
        score_style = ParagraphStyle('ScoreMain', fontSize=11, textColor=dark_grey, alignment=TA_CENTER, fontName='Helvetica-Bold', spaceBefore=8)
        breakdown_elements.append([Paragraph(f"Overall: {total_score} out of {max_score} points ({percentage:.0f}%)", score_style)])
        
        context_style = ParagraphStyle('ScoreContext', fontSize=9, textColor=medium_grey, alignment=TA_CENTER, spaceAfter=15)
        breakdown_elements.append([Paragraph(score_context, context_style)])
        
        score_rows = []
        label_style = ParagraphStyle('ScoreLabel', fontSize=9, textColor=dark_grey, fontName='Helvetica-Bold')
        value_style = ParagraphStyle('ScoreValue', fontSize=8, textColor=medium_grey)
        
        if pairs_completed.get('pair1'):
            p1 = visual_analysis.get('pair1_scores', {})
            size = p1.get('size_difference', {}).get('score', 0)
            colour = p1.get('colour_differentiation', {}).get('score', 0)
            marks = p1.get('angularity', {}).get('score', 0)
            density = p1.get('density', {}).get('score', 0)
            shape = p1.get('shape_features', {}).get('score', 0)
            score_rows.append([
                Paragraph("Name and Self Portrait", label_style),
                Paragraph(f"Size {size}/3", value_style),
                Paragraph(f"Colour {colour}/2", value_style),
                Paragraph(f"Marks {marks}/2", value_style),
                Paragraph(f"Shape {shape}/1", value_style)
            ])
        
        if pairs_completed.get('pair2'):
            p2 = visual_analysis.get('pair2_scores', {})
            size = p2.get('size_difference', {}).get('score', 0)
            colour = p2.get('colour_object_appropriate', {}).get('score', 0)
            marks = p2.get('angularity', {}).get('score', 0)
            shape = p2.get('shape_features', {}).get('score', 0)
            score_rows.append([
                Paragraph("Sun Writing and Drawing", label_style),
                Paragraph(f"Size {size}/3", value_style),
                Paragraph(f"Colour {colour}/3", value_style),
                Paragraph(f"Marks {marks}/2", value_style),
                Paragraph(f"Shape {shape}/3", value_style)
            ])
        
        if pairs_completed.get('pair1') and pairs_completed.get('pair2'):
            cp = visual_analysis.get('cross_pair_scores', {})
            if cp:
                writing = cp.get('writing_consistency', {}).get('score', 0)
                drawing = cp.get('drawing_variety', {}).get('score', 0)
                score_rows.append([
                    Paragraph("Consistency", label_style),
                    Paragraph(f"Writing {writing}/2", value_style),
                    Paragraph(f"Drawing {drawing}/1", value_style),
                    Paragraph("", value_style),
                    Paragraph("", value_style)
                ])
        
        if score_rows:
            scores_table = Table(score_rows, colWidths=[120, 70, 70, 70, 70])
            scores_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LINEABOVE', (0, 0), (-1, 0), 0.5, HexColor('#DDDDDD')),
                ('LINEBELOW', (0, -1), (-1, -1), 0.5, HexColor('#DDDDDD')),
            ]))
            breakdown_elements.append([scores_table])
    
        breakdown_box = Table(breakdown_elements, colWidths=[available_width - 40])
        breakdown_box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), HexColor('#FAFAFA')),
            ('BOX', (0, 0), (-1, -1), 1, HexColor('#E0E0E0')),
            ('TOPPADDING', (0, 0), (-1, 0), 15),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 15),
            ('LEFTPADDING', (0, 0), (-1, -1), 20),
            ('RIGHTPADDING', (0, 0), (-1, -1), 20),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        story.append(breakdown_box)
    
    # Footer
    story.append(Spacer(1, 30))
    
    footer_style = ParagraphStyle('Footer', fontSize=8, textColor=medium_grey, alignment=TA_CENTER, leading=12)
    story.append(Paragraph("This report is informed by peer-reviewed research into how young children develop", footer_style))
    story.append(Paragraph("an understanding that writing and drawing are different.", footer_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Questions? contact@morehandwriting.co.uk  •  morehandwriting.co.uk", footer_style))
    
    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


def build_pdf_strengths(visual_analysis: dict, child_name: str, stage: str, age_months: int) -> list:
    """Build list of strengths for PDF based on visual analysis scores and developmental context."""
    
    # For conventional writers, return different strengths
    if stage == "ALREADY WRITING":
        return [
            f"{child_name} is writing real, recognisable letters - this is a wonderful achievement!",
            f"{child_name} clearly understands that writing and drawing are different activities.",
            f"{child_name} has moved beyond the early mark-making stage into real letter writing.",
            f"{child_name}'s writing shows they are ready for activities that build on this strong foundation."
        ]
    
    strengths = []
    observations = visual_analysis.get('observations', {})
    
    obs1 = str(observations.get('name_writing', '')).lower()
    obs2 = str(observations.get('self_portrait', '')).lower()
    obs3 = str(observations.get('sun_writing', '')).lower()
    obs4 = str(observations.get('sun_drawing', '')).lower()
    
    pair1_identical = 'identical' in obs1 or 'identical' in obs2 or 'same as' in obs1 or 'same as' in obs2
    pair2_identical = 'identical' in obs3 or 'identical' in obs4 or 'same as' in obs3 or 'same as' in obs4
    
    # Add strengths based on scores
    if visual_analysis.get('pair1_scores') and not pair1_identical:
        p1 = visual_analysis['pair1_scores']
        if p1.get('size_difference', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} made their name writing smaller than their self-portrait, showing an understanding that writing typically takes up less space than pictures.")
        if p1.get('colour_differentiation', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} chose darker colours for writing their name, showing awareness that writing and drawing are different activities.")
        if p1.get('angularity', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} used more angular marks for writing and curved marks for drawing, reflecting how real writing and pictures look different.")
        if p1.get('shape_features', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} included letter-like shapes in their name writing attempt.")
    
    if visual_analysis.get('pair2_scores') and not pair2_identical:
        p2 = visual_analysis['pair2_scores']
        if p2.get('size_difference', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} wrote 'sun' smaller than they drew the sun, showing awareness that words and pictures have different sizes.")
        if p2.get('colour_object_appropriate', {}).get('score', 0) >= 2:
            strengths.append(f"{child_name} used appropriate colours - darker for writing and brighter for the sun drawing.")
        if p2.get('shape_features', {}).get('score', 0) >= 1 and p2.get('size_difference', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} drew a recognisable sun shape while making their writing look distinctly different.")
    
    if visual_analysis.get('cross_pair_scores') and not pair1_identical and not pair2_identical:
        cp = visual_analysis['cross_pair_scores']
        if cp.get('writing_consistency', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} showed consistency in their approach to writing across different tasks.")
        if cp.get('drawing_variety', {}).get('score', 0) >= 1:
            strengths.append(f"{child_name} created appropriately different drawings for different subjects.")
    
    strongest = str(visual_analysis.get('strongest_evidence', '')).lower()
    if strongest and 'identical' not in strongest and 'same image' not in strongest and 'same as' not in strongest:
        strengths.append(visual_analysis.get('strongest_evidence'))
    
    # For Early Days, provide age-contextualised encouragement
    if len(strengths) == 0:
        if age_months < 32:
            strengths.append(f"{child_name} is engaging with mark-making activities, which is the essential first step before children begin to differentiate between writing and drawing.")
            strengths.append(f"At this age, {child_name}'s focus on making marks - regardless of what they look like - is exactly what builds the foundation for later writing development.")
        else:
            strengths.append(f"{child_name} willingly engaged with both the writing and drawing tasks.")
            strengths.append(f"{child_name} is using consistent approaches to mark-making, which shows developing motor control.")
    
    return strengths[:4]


def colours_match(colour1: str, colour2: str) -> bool:
    """
    Check if two colour names are semantically the same.
    Handles colour families (e.g., 'navy' matches 'dark blue').
    """
    if not colour1 or not colour2:
        return False
    
    colour1 = colour1.lower().strip()
    colour2 = colour2.lower().strip()
    
    # Exact match
    if colour1 == colour2:
        return True
    
    # Word boundary partial match (avoid "red" matching "bored")
    import re
    # Check if one is a complete word within the other
    if re.search(rf'\b{re.escape(colour1)}\b', colour2) or re.search(rf'\b{re.escape(colour2)}\b', colour1):
        return True
    
    # Colour families (FIXED: removed duplications)
    colour_families = {
        'blue': ['navy', 'dark blue', 'light blue', 'royal blue', 'sky blue', 'turquoise', 'teal', 'cyan'],
        'red': ['dark red', 'crimson', 'scarlet', 'maroon', 'burgundy'],
        'yellow': ['gold', 'golden', 'lemon'],
        'green': ['dark green', 'light green', 'lime', 'olive', 'forest green'],
        'grey': ['gray', 'silver', 'charcoal'],  # charcoal only here
        'purple': ['violet', 'lavender', 'lilac', 'mauve', 'plum'],
        'pink': ['rose', 'fuchsia', 'magenta', 'hot pink'],
        'orange': ['coral', 'peach', 'tangerine'],
        'brown': ['tan', 'beige', 'chocolate', 'bronze'],
        'black': ['ebony'],  # removed charcoal from here
        'white': ['ivory', 'pearl', 'cream']  # cream moved here from yellow
    }
    
    # Check if both colours belong to the same family
    for base_colour, variations in colour_families.items():
        if ((colour1 == base_colour or colour1 in variations) and 
            (colour2 == base_colour or colour2 in variations)):
            return True
    
    return False




def generate_comprehensive_summary(visual_analysis: dict, child_name: str, stage: str, age_months: int, favourite_colour_stated: str = '') -> str:
    """Generate a detailed, research-informed summary paragraph for the PDF report.
    
    Links findings to the research benchmark of 2 years 8 months and provides
    age-appropriate context.
    """
    observations = visual_analysis.get('observations', {})
    
    age_years = age_months // 12
    age_remainder = age_months % 12
    age_string = f"{age_years} years"
    if age_remainder > 0:
        age_string += f" and {age_remainder} months"
    
    # Handle conventional writers first
    if stage == "ALREADY WRITING":
        writing_stage_reasoning = visual_analysis.get('writing_stage_reasoning', '')
        favourite_colour_detected = visual_analysis.get('favourite_colour_detected', 'none')  # ADD THIS LINE
        
        summary = f"Great news! At {age_string} old, {child_name} is already writing real, recognisable letters. "
        summary += "This assessment is designed for children who are still learning that writing and drawing are different - "
        summary += f"but {child_name} has already mastered this understanding and moved into real letter writing. "
        if writing_stage_reasoning:
            summary += f"Specifically, we observed: {writing_stage_reasoning} "
        
        # ADD THIS ENTIRE BLOCK:        
     
       # Smart favourite colour detection - with robust checks
        if favourite_colour_detected and favourite_colour_detected.lower() not in ['none', '', 'n/a', 'null']:
            if favourite_colour_stated and favourite_colour_stated.lower() not in ['', 'no_preference', 'none']:
                # Parent stated a preference
                if colours_match(favourite_colour_detected, favourite_colour_stated):
                    summary += f"You mentioned that {favourite_colour_detected} is {child_name}'s favourite colour, and we can see this reflected in the samples. "
                else:
                    summary += f"You mentioned {favourite_colour_stated} as {child_name}'s favourite colour. In these samples, we noticed {favourite_colour_detected} appearing frequently. "
            else:
                # No parent input, but AI detected a colour
                summary += f"We noticed {favourite_colour_detected} appearing frequently across the samples. "
                
        summary += f"The numerical score is not really meaningful for {child_name} at this stage - what matters is that they have a strong foundation to build on. The activities below are designed to extend their existing skills."
        return summary
        
    obs1 = str(observations.get('name_writing', '')).lower()
    obs2 = str(observations.get('self_portrait', '')).lower()
    obs3 = str(observations.get('sun_writing', '')).lower()
    obs4 = str(observations.get('sun_drawing', '')).lower()
    
    pair1_identical = 'identical' in obs1 or 'identical' in obs2 or 'same as' in obs1 or 'same as' in obs2
    pair2_identical = 'identical' in obs3 or 'identical' in obs4 or 'same as' in obs3 or 'same as' in obs4
    both_identical = pair1_identical and pair2_identical
    
    p1_scores = visual_analysis.get('pair1_scores', {})
    p2_scores = visual_analysis.get('pair2_scores', {})
    
    if stage == "STRONG START":
        summary = f"At {age_string} old, {child_name} is demonstrating a clear understanding that writing and drawing are different. "
        
        differences = []
        if p1_scores.get('size_difference', {}).get('score', 0) >= 2 or p2_scores.get('size_difference', {}).get('score', 0) >= 2:
            differences.append("making writing smaller than drawings")
        if p1_scores.get('colour_differentiation', {}).get('score', 0) >= 2 or p2_scores.get('colour_object_appropriate', {}).get('score', 0) >= 2:
            differences.append("choosing different colours for writing and drawing")
        if p1_scores.get('angularity', {}).get('score', 0) >= 2 or p2_scores.get('angularity', {}).get('score', 0) >= 2:
            differences.append("using different types of marks")
        
        if differences:
            summary += f"This is evident in {' and '.join(differences)}. "
        
        summary += "Research has found that children typically begin to make this distinction from around 2 years and 8 months. "
        summary += f"{child_name} is showing exactly the kind of emerging literacy awareness that supports later reading and writing development. "
        summary += "The activities below will help build on this strong foundation."
        
    elif stage == "BEGINNING EXPLORER":
        summary = f"At {age_string} old, {child_name} is beginning to show awareness that writing and drawing are different activities. "
        
        emerging = []
        if p1_scores.get('size_difference', {}).get('score', 0) >= 1 or p2_scores.get('size_difference', {}).get('score', 0) >= 1:
            emerging.append("some variation in the size of marks")
        if p1_scores.get('colour_differentiation', {}).get('score', 0) >= 1 or p2_scores.get('colour_object_appropriate', {}).get('score', 0) >= 1:
            emerging.append("different colour choices")
        if p1_scores.get('shape_features', {}).get('score', 0) >= 1 or p2_scores.get('shape_features', {}).get('score', 0) >= 1:
            emerging.append("early attempts at letter-like shapes")
        
        if emerging:
            summary += f"The samples show {' and '.join(emerging)}, which indicates this understanding is developing. "
        
        summary += "Research has found that children typically begin distinguishing writing from drawing from around 2 years and 8 months. "
        summary += f"{child_name} is on this developmental path and the activities suggested below will support continued progress."
        
    else:  # EARLY DAYS
        summary = f"At {age_string} old, {child_name}'s writing and drawing samples look quite similar. "
        
        if both_identical:
            summary += f"When asked to write and draw, {child_name} produced very similar marks for both tasks. "
        
        # Age-specific context based on the 2y8m benchmark
        if age_months < 32:
            # Under 2y8m
            summary += "Research has found that children typically begin to distinguish between writing and drawing from around 2 years and 8 months. "
            summary += f"At {child_name}'s current age, the focus should be on enjoying making marks and having positive experiences with crayons, pencils and paper. "
            summary += "This builds the foundation for later differentiation."
        elif age_months < 42:
            # 2y8m to 3y6m
            summary += "Research has found that children typically begin to distinguish between writing and drawing from around 2 years and 8 months, though this varies between children. "
            summary += f"{child_name} would benefit from activities that naturally highlight the differences between writing and drawing - such as pointing out words and pictures during story time. "
            summary += "The activities below are designed to support this emerging awareness."
        else:
            # 3y6m to 4y
            summary += "Research has found that most children begin to distinguish between writing and drawing from around 2 years and 8 months. "
            summary += f"With regular exposure to books, print and the activities suggested below, {child_name} will develop this understanding. "
            summary += "Every child develops at their own pace and with the right support, progress at this stage is often rapid."
               
    # Add favourite colour note for all non-conventional stages
    favourite_colour_detected = visual_analysis.get('favourite_colour_detected', 'none')
    if favourite_colour_detected and favourite_colour_detected.lower() not in ['none', '', 'n/a', 'null']:
        if favourite_colour_stated and favourite_colour_stated.lower() not in ['', 'no_preference', 'none']:
            if colours_match(favourite_colour_detected, favourite_colour_stated):
                summary += f" We noticed {child_name} used {favourite_colour_detected} frequently — their favourite colour!"
            else:
                summary += f" We noticed {favourite_colour_detected} appearing frequently across the samples."
        else:
            summary += f" We noticed {favourite_colour_detected} appearing frequently across the samples."
    
    return summary

# =============================================================================
# ACTIVITY POOL - 50+ Research-Based Activities
# =============================================================================

ACTIVITY_POOL = [
    # === MARK-MAKING FOUNDATIONS (Ages 24-30m, Early Days) ===
    {
        "title": "Enjoying Mark-Making Together",
        "description": "The most important thing right now is for {child_name} to enjoy making marks. Offer crayons, chalk, finger paint - whatever {child_name} likes. Sit alongside and make your own marks. Say 'I love your marks!' without asking what they are or trying to correct them.",
        "research_note": "Children who have positive early experiences with mark-making are more likely to engage with writing later.",
        "age_bands": ["24-30"],
        "stages": ["scribbles"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Big Movements, Big Marks",
        "description": "Give {child_name} very large paper (or tape several sheets together) and chunky crayons. Encourage big arm movements - circles, swooshes, up and down. This builds the shoulder and arm muscles needed for later writing control.",
        "research_note": None,
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },

{
        "title": "Outdoor Mark-Making Adventures",
        "description": "Let {child_name} make marks outdoors: chalk on paving stones, paintbrushes with water on fences, or smooth twigs in mud (supervise carefully with sticks to prevent eye injuries). Different surfaces and tools build control and confidence.",
        "research_note": "Varied mark-making experiences develop motor skills and creative confidence. Always supervise when using sticks or twigs.",
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Looking at Books Together",
        "description": "Share books with {child_name} every day, even just for a few minutes. Let {child_name} turn the pages and point at things. Occasionally point to the words as you read a line. This exposure to print builds familiarity that will support later learning.",
        "research_note": "Frequency of shared reading is one of the strongest predictors of later literacy.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Pointing Out Print Naturally",
        "description": "Throughout the day, casually point to words: 'This says OPEN', 'Look, this word tells us it is milk.' Keep it very brief and natural - just a sentence here and there. This helps {child_name} begin to notice that print is everywhere.",
        "research_note": None,
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["print_awareness"],
        "types": ["indoor", "outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Making Marks with Purpose",
        "description": "Give {child_name} reasons to make marks: 'Can you draw what you would like for dinner?' or 'Let us make marks on this card for Grandma.' Even if the marks do not look like anything recognisable, responding to them as meaningful ('Oh, you want pasta!') helps {child_name} understand that marks carry meaning.",
        "research_note": "Children who understand that marks carry meaning make faster progress in learning to write.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Circular Scribbles Practice",
        "description": "Encourage {child_name} to make big circular scribbles - going round and round on paper. This circular motion is crucial for later letter formation. Make it fun by pretending to stir a giant pot or draw the sun going round and round.",
        "research_note": "Circular scribbles are a key developmental milestone that precedes letter formation.",
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "You Draw, I Will Write",
        "description": "After {child_name} draws a picture, offer to write a word or sentence about it underneath. Make it visible by saying 'You drew a lovely house - I will write the word house here. See how my writing looks different from your picture?' This models the relationship between pictures and words.",
        "research_note": "Seeing adults write in response to drawings helps children understand that writing carries meaning.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },

{
        "title": "Lots of Different Mark-Making Tools",
        "description": "Offer varied tools: chunky crayons, chalk, paintbrushes, sticks in sand. Let {child_name} see you use the same tools. Children develop control through practice with different materials.",
        "research_note": "Fine motor development through varied mark-making supports later handwriting.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "outdoor", "limited_resources"]
    },
    {
        "title": "Writing and Drawing Side by Side",
        "description": "When {child_name} makes a picture, sit beside them and write a word related to their drawing on the same page. Point out how they look different: 'You drew a big sun, and I wrote a little word - sun.' This contrast helps children see the difference between pictures and writing.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === PRINT AWARENESS & DIFFERENTIATION (Ages 30-42m, Beginning Explorer/Strong Start) ===
    {
        "title": "Words and Pictures at Story Time",
        "description": "When you read together, occasionally run your finger under the words as you read a line. Point to a picture and say 'Look at this!' then point to the words and say 'And these are the words that tell us about it.' Keep it brief - the story is what matters most.",
        "research_note": "Research shows that simply drawing attention to print helps children begin to notice it.",
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["print_awareness", "differentiation"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Noticing Words and Pictures",
        "description": "During story time, occasionally pause to point out the difference between words and pictures. You might say 'Look, here is a picture of a dog, and this word here says dog.' After a page, you could ask {child_name}: 'Can you point to some words? Can you point to a picture?' Keep it playful.",
        "research_note": "Research shows that this kind of 'print referencing' during shared reading significantly boosts print awareness.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["print_awareness", "differentiation"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Environmental Print Hunt",
        "description": "When you are out, point out print naturally: 'Look, that sign says STOP' or 'This packet says biscuits.' Ask {child_name} to spot letters from their name on signs or packaging. Children who notice print in their environment develop stronger literacy foundations.",
        "research_note": "Children who can recognise environmental print show stronger early reading skills.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging", "conventional"],
        "skills": ["print_awareness", "letter_knowledge"],
        "types": ["outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Pretend Writing in Play",
        "description": "Set up a pretend post office, café or shop with paper, envelopes and pencils. Let {child_name} 'write' orders, letters or receipts as part of play. This purposeful mark-making is more valuable than practice sheets.",
        "research_note": "Research shows that children who engage in play-based writing develop better understanding of print functions.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["purposeful_writing", "play"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "Thinking Aloud When You Write",
        "description": "When you write a shopping list, card or note, let {child_name} watch. Think aloud: 'I need to remember to buy milk, so I am writing M-I-L-K.' Children learn from seeing adults use print for real purposes.",
        "research_note": "Children whose parents model everyday writing show greater understanding of print's purpose.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["print_awareness", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === NAME WRITING FOCUS (Ages 30-42m, All Stages) ===
    {
        "title": "Writing {child_name}'s Name",
        "description": "Help {child_name} practise writing their name using chunky crayons and big paper. Point out letters: 'Your name starts with this letter - can you spot it anywhere else today?' Celebrate all attempts - the process matters more than perfection.",
        "research_note": "Name writing is one of the strongest predictors of later literacy success.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Name Recognition Games",
        "description": "Write {child_name}'s name on cards or sticky notes and hide them around the house. Ask {child_name} to find them. Talk about the letters: 'Your name starts with... Can you find that letter?' This makes letter learning playful.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "Tracing Over Your Writing",
        "description": "Write {child_name}'s name in thick crayon, then let them trace over it with a different colour. This helps them feel the movements needed for each letter. Gradually fade support: thick tracing → thin lines → dots → independent.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "fine_motor"],
        "types": ["indoor", "limited_resources", "with_help"]
    },

# === LETTER KNOWLEDGE & PHONICS (Ages 30-42m, Emerging/Conventional) ===
    {
        "title": "Letter Sounds in Daily Life",
        "description": "Point out letter sounds naturally: 'Ball starts with buh - B!' or 'Your name starts with... what sound?' Keep it casual and follow {child_name}'s interest. A few seconds here and there builds awareness without pressure.",
        "research_note": "Children who learn letter sounds alongside letter names show stronger reading development.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["phonics", "letter_knowledge"],
        "types": ["indoor", "outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Alphabet Books and Songs",
        "description": "Share alphabet books with {child_name}, singing the ABC song together. Point to letters as you sing. Choose books with clear, simple pictures for each letter. This makes letter learning multi-sensory and fun.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "phonics"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Making Letters with Play-Dough",
        "description": "Roll play-dough into 'snakes' and help {child_name} shape them into letters, starting with letters from their name. Talk about the shapes: 'We make a line down, then a curve' for P. This tactile approach helps letter formation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "fine_motor"],
        "types": ["indoor", "play"]
    },
    {
        "title": "Letter Hunt in Books",
        "description": "Pick a letter (start with the first letter of {child_name}'s name) and hunt for it together in a favourite book. Count how many you find. This builds letter recognition in a natural context.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help", "play"]
    },
  {
        "title": "Magnetic Letters on the Fridge",
        "description": "Keep large magnetic letters (at least 2 inches / 5cm tall) on the fridge at {child_name}'s height. Show them the letters in their name and let them arrange them. Talk about the letters casually while cooking: 'Can you find the M? That is for Mummy/Mum.' Always supervise to ensure letters stay on the fridge and are not mouthed.",
        "research_note": "Use only magnetic letters that are at least 2 inches (5cm) tall to reduce choking risk. Not suitable for children who still mouth objects frequently. Adult supervision required.",
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "name_writing"],
        "types": ["indoor", "with_help", "play"]
    },
    
    # === FINE MOTOR & HAND STRENGTH (All Ages) ===
    {
        "title": "Threading and Lacing with Jumbo Beads",
        "description": "Threading JUMBO beads (at least 1.5 inches / 4cm in size) or large lacing cards builds the finger strength and control needed for writing. Use beads specifically designed for toddlers with thick laces. Let {child_name} work at their own pace - the process builds skills even if they do not complete the pattern. ALWAYS supervise closely.",
        "research_note": "Use only JUMBO beads (1.5 inches or larger) designed for toddlers. Fine motor activities like threading significantly improve pencil control. Adult supervision required at all times to prevent choking hazards.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["fine_motor"],
        "types": ["indoor", "with_help"]
    },
    {
        "title": "Tearing and Crumpling Paper",
        "description": "Let {child_name} tear newspaper into strips or crumple paper into balls. These simple activities build hand strength. Make it purposeful: 'Let us make confetti!' or 'Can you scrunch this really tight?'",
        "research_note": None,
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor"],
        "types": ["indoor", "limited_resources", "independent"]
    },
    {
        "title": "Playdough Squeezing and Rolling",
        "description": "Playing with playdough - squeezing, rolling, pinching - builds hand muscles essential for writing. No need for specific shapes; free exploration is valuable. Five minutes of playdough play strengthens hands for holding pencils.",
        "research_note": "Hand strengthening activities directly improve writing stamina and control.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["fine_motor"],
        "types": ["indoor", "play"]
    },
    {
        "title": "Using Tweezers and Tongs",
        "description": "Let {child_name} use child-safe tweezers or kitchen tongs to pick up pom-poms, cotton balls or small toys. This pincer grip is exactly what they need for holding a pencil. Make it a game: 'Can you move all the balls to this bowl?'",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["fine_motor"],
        "types": ["indoor", "play"]
    },
    {
        "title": "Painting at an Easel",
        "description": "Painting on a vertical surface (easel or paper taped to a wall) builds shoulder stability and wrist strength - both crucial for writing. Large brushstrokes develop arm control that later translates to pencil control.",
        "research_note": "Vertical surface work strengthens the shoulder muscles needed for stable handwriting.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "outdoor"]
    },
    
    # === CONVENTIONAL WRITERS (Ages 36-42m) ===
    {
        "title": "Building on Strong Foundations",
        "description": "Wonderful news - {child_name} is already writing real letters! This assessment is designed for children still learning that writing and drawing are different, so the scoring is not really relevant for {child_name}. Instead, focus on activities that build on their existing skills.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["name_writing", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
 
    {
        "title": "Simple Words and Labels",
        "description": "Encourage {child_name} to write simple words - labels for their drawings, names of family members, or words from their favourite books. Keep it playful and pressure-free.",
        "research_note": "Children who write for real purposes develop stronger motivation for literacy.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Reading Together for Comprehension",
        "description": "Since {child_name} already recognises that print carries meaning, story time can focus on enjoying stories together. Ask questions like 'What do you think will happen next?' or 'Why did the character do that?' to build comprehension skills.",
        "research_note": "Reading comprehension develops best through discussion and engagement with stories.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    # === CONVENTIONAL WRITERS - EXTENDED ACTIVITIES ===
    {
        "title": "Writing Simple Words",
        "description": "Encourage {child_name} to write simple 3-letter words like 'cat', 'dog', 'sun', 'mum', 'dad'. Sound out the letters together: 'c-a-t'. Celebrate every attempt - spelling will develop naturally with practice.",
        "research_note": "Children who attempt to write words, even with invented spellings, develop stronger phonemic awareness.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "phonics", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Writing Messages to Family",
        "description": "Help {child_name} write short messages or cards to family members - 'I love you', 'Happy Birthday', or just names. Real purposes for writing build motivation. Accept invented spellings warmly.",
        "research_note": "Writing for authentic purposes significantly increases children's motivation to write.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "name_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Labelling Drawings with Words",
        "description": "After {child_name} draws a picture, encourage them to write a word or two to label it - the name of what they drew, or a simple describing word. This connects drawing and writing purposefully.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Creating a Name Collection",
        "description": "Help {child_name} create a collection of names they can write - their name, family members, pets, friends. Make a special 'Names I Can Write' book or poster to display proudly.",
        "research_note": "Name writing extends naturally to writing other meaningful names in the child's life.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["name_writing", "letter_knowledge", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Writing a Shopping List Together",
        "description": "Before shopping, make a list together. Let {child_name} write words they know (milk, eggs, bread) while you help with harder ones. Use the list at the shop - this shows writing has real purpose.",
        "research_note": "Functional writing tasks build understanding that writing serves practical purposes.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Keeping a Simple Journal",
        "description": "Give {child_name} a special notebook for drawing and writing about their day. They might draw a picture and write one word or a short sentence underneath. Even 'I went park' is wonderful progress.",
        "research_note": "Regular, low-pressure writing practice builds confidence and fluency.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "fine_motor"],
        "types": ["indoor", "limited_resources", "independent"]
    },
    {
        "title": "Making Labels for Their Room",
        "description": "Help {child_name} make labels for things in their room - 'bed', 'toys', 'books', 'door'. They write the words on card and you help attach them. This creates a print-rich environment they created themselves.",
        "research_note": "Child-created environmental print reinforces the connection between spoken and written words.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "letter_knowledge", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Writing Thank You Notes",
        "description": "After receiving a gift or kindness, help {child_name} write a simple thank you note. They write what they can (their name, 'thank you', the person's name) and you help with the rest.",
        "research_note": "Thank you notes teach both social skills and purposeful writing simultaneously.",
        "age_bands": ["36-42"],
        "stages": ["conventional"],
        "skills": ["purposeful_writing", "name_writing"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    
# === CREATIVE & PLAYFUL APPROACHES (All Ages, All Stages) ===
 {
        "title": "Writing on a Chalkboard or Whiteboard",
        "description": "Give {child_name} a small chalkboard with chunky chalk, or a whiteboard with washable markers. Let them make marks, lines, circles and letter shapes. This erasable surface is forgiving - mistakes disappear with a wipe! Much safer than sand for this age group.",
        "research_note": "Vertical surfaces like chalkboards and whiteboards build shoulder stability essential for handwriting control.",
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "fine_motor", "letter_knowledge"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Rainbow Writing",
        "description": "Write {child_name}'s name in light pencil or crayon. Let them trace over it multiple times with different colours, creating a rainbow effect. This repetition builds muscle memory for letter formation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["name_writing", "fine_motor"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Writing Letters in the Air",
        "description": "Stand together and 'write' big letters in the air with your whole arm. Say the letter name as you form it. This builds spatial awareness and letter memory without the pressure of paper.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "fine_motor"],
        "types": ["indoor", "outdoor", "limited_resources", "play"]
    },
    {
        "title": "Making a Mark-Making Station",
        "description": "Set up a small table or corner with paper, crayons, pencils and markers always available. When {child_name} is interested, they can choose to make marks independently. Low-pressure availability encourages exploration.",
        "research_note": None,
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "purposeful_writing"],
        "types": ["indoor", "limited_resources", "independent"]
    },
    {
        "title": "Drawing What You See",
        "description": "When out on walks, sit together and let {child_name} draw things they can see - a tree, a car, a house. This purposeful drawing builds observation skills and hand control. No pressure for accuracy - the process matters.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["outdoor", "limited_resources"]
    },
    {
        "title": "Making Birthday Cards and Notes",
        "description": "When someone has a birthday or you want to say thank you, help {child_name} make a card. They can draw the picture while you write the words, or they can add their name. This shows writing serving a real purpose.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging", "conventional"],
        "skills": ["purposeful_writing", "mark_making"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === BUILDING DIFFERENTIATION AWARENESS (Ages 30-42m) ===
    {
        "title": "Comparing Writing and Drawing",
        "description": "After {child_name} makes marks, occasionally compare them: 'When you draw, you make big colourful pictures. When you write, you make small dark marks in a line.' Keep it observational, not corrective.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Sorting Pictures and Words",
        "description": "Cut out pictures and words from magazines or newspapers. Help {child_name} sort them into two piles: 'pictures' and 'words'. Talk about how they look different. This concrete sorting builds differentiation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "play"]
    },
    {
        "title": "Two Pages: One for Drawing, One for Writing",
        "description": "Give {child_name} two sheets of paper side by side. Say 'This one is for drawing a big picture, and this one is for writing small marks.' Let them choose their own approach - the physical separation helps clarify the difference.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Noticing Writing is Smaller",
        "description": "When reading books, occasionally point out: 'Look how tiny these words are! And look how big this picture is!' This repeated observation helps {child_name} notice that writing and pictures typically differ in size.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["differentiation", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    
    # === MOTOR CONTROL & PRE-WRITING PATTERNS (Ages 24-36m) ===
    {
        "title": "Dot-to-Dot Lines",
        "description": "Make simple dot patterns for {child_name} to connect: two dots to make a line, three dots to make a triangle, dots in a row. This builds the control needed for forming letters.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Making Zigzags and Waves",
        "description": "Show {child_name} how to make zigzag lines and wavy lines across the page. These pre-writing patterns build the hand control needed for letters. Make it fun: 'Draw the mountain path!' or 'Make waves in the sea!'",
        "research_note": None,
        "age_bands": ["24-30", "30-36"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Copying Simple Shapes",
        "description": "Draw simple shapes - circle, line, cross - and let {child_name} try to copy them nearby. These basic shapes are the building blocks of letters. Praise effort, not perfection.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["fine_motor", "mark_making"],
        "types": ["indoor", "limited_resources"]
    },
    
    # === EMERGENT LITERACY ACTIVITIES (Ages 36-42m, Emerging/Conventional) ===
    {
        "title": "Writing Shopping Lists Together",
        "description": "Before shopping, write a list together. You write the words while {child_name} adds their marks or attempts at letters beside each item. This shows writing serving a practical purpose.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["emerging", "conventional"],
        "skills": ["purposeful_writing", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Labelling Toy Boxes",
        "description": "Help {child_name} make labels for their toy boxes. They can draw a picture of what goes inside, and you (or they) can add the word. This practical writing makes organisation meaningful.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["emerging", "conventional"],
        "skills": ["purposeful_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Family Name Writing",
        "description": "Help {child_name} learn to write the names of family members, starting with short names. Make name cards they can trace or copy. Celebrating their attempts to write 'Mum', 'Dad', or siblings' names builds motivation.",
        "research_note": None,
        "age_bands": ["36-42"],
        "stages": ["emerging", "conventional"],
        "skills": ["name_writing", "letter_knowledge"],
        "types": ["indoor", "limited_resources"]
    },
# === ENGAGING WITH BOOKS & STORIES (All Ages) ===
{
        "title": "Making Your Own Books",
        "description": "Fold a few sheets of paper in half to make a simple book. Secure the pages with tape or paperclips, or leave them loose (avoid staples which can injure small fingers). Let {child_name} draw pictures on each page while you write a word or sentence about each picture. Read the book together. This shows writing and pictures working together.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging", "conventional"],
        "skills": ["purposeful_writing", "print_awareness"],
        "types": ["indoor", "limited_resources", "with_help"]
    },
    {
        "title": "Retelling Stories with Drawings",
        "description": "After reading a favourite story, give {child_name} paper to draw what happened. Ask them to tell you about their drawing. This connects stories with mark-making and builds narrative skills.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["scribbles", "letter_like", "emerging"],
        "skills": ["mark_making", "purposeful_writing"],
        "types": ["indoor", "limited_resources"]
    },
    {
        "title": "Letter of the Week",
        "description": "Choose one letter to focus on for a week - perhaps from {child_name}'s name. Point it out in books, on signs, on food packaging. Make that letter shape together. This repeated, relaxed exposure builds letter knowledge.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "print_awareness"],
        "types": ["indoor", "outdoor", "limited_resources", "with_help"]
    },
    {
        "title": "Texture Writing",
        "description": "Place paper over different textures (tree bark, fabric, coins under paper) and let {child_name} rub a crayon over it. Talk about how different surfaces create different marks. This builds awareness of mark-making possibilities.",
        "research_note": None,
        "age_bands": ["24-30", "30-36", "36-42"],
        "stages": ["scribbles", "letter_like"],
        "skills": ["mark_making", "fine_motor"],
        "types": ["indoor", "outdoor", "limited_resources"]
    },
    {
        "title": "Giant Outdoor Chalk Letters",
        "description": "Using chunky pavement chalk, draw giant letters on the ground outside. Let {child_name} walk along the letter shapes with their feet. This whole-body approach helps letter recognition and formation.",
        "research_note": None,
        "age_bands": ["30-36", "36-42"],
        "stages": ["letter_like", "emerging"],
        "skills": ["letter_knowledge", "fine_motor"],
        "types": ["outdoor", "limited_resources", "play"]
    },
]

def get_recommended_activities(stage: str, score: int, child_name: str = "your child", age_months: int = 36, email: str = "") -> list:
    """Return 4-5 activities from ACTIVITY_POOL using prioritized scoring and randomization."""
    import random
    
    # Determine age band
    if age_months <= 30:
        age_band = "24-30"
    elif age_months <= 36:
        age_band = "30-36"
    else:
        age_band = "36-42"
    
    # Map stage to stage tags
    stage_clean = stage.upper().replace(" ", "_")
    if stage_clean == "ALREADY_WRITING":
        stage_tags = ["conventional"]
        is_conventional = True
    elif stage_clean == "STRONG_START":
        stage_tags = ["emerging"]
        is_conventional = False
    elif stage_clean == "BEGINNING_EXPLORER":
        stage_tags = ["letter_like", "emerging"]
        is_conventional = False
    else:
        stage_tags = ["scribbles", "letter_like"]
        is_conventional = False
    
    # Determine priority skills based on stage
    if stage_clean == "ALREADY_WRITING":
        priority_skills = ["purposeful_writing", "name_writing", "letter_knowledge", "phonics"]
    elif stage_clean == "STRONG_START":
        priority_skills = ["differentiation", "letter_knowledge", "name_writing", "print_awareness"]
    elif stage_clean == "BEGINNING_EXPLORER":
        priority_skills = ["differentiation", "purposeful_writing", "print_awareness", "letter_knowledge"]
    else:
        priority_skills = ["mark_making", "fine_motor", "print_awareness"]
    
    # Score each activity
    scored_activities = []
    for activity in ACTIVITY_POOL:
        score_total = 0
        
        # Age match (0-3 points)
        if age_band in activity["age_bands"]:
            score_total += 3
        else:
            adjacent_match = False
            if age_band == "24-30" and "30-36" in activity["age_bands"]:
                adjacent_match = True
            elif age_band == "30-36" and ("24-30" in activity["age_bands"] or "36-42" in activity["age_bands"]):
                adjacent_match = True
            elif age_band == "36-42" and "30-36" in activity["age_bands"]:
                adjacent_match = True
            
            if adjacent_match:
                score_total += 2
            elif len(activity["age_bands"]) >= 2:
                score_total += 1
        
        # Stage match - HEAVILY weight conventional for conventional writers
        stage_overlap = set(stage_tags) & set(activity["stages"])
        
        if is_conventional:
            if "conventional" in activity["stages"]:
                score_total += 5  # Big bonus for conventional-specific
            elif stage_overlap:
                score_total += 2
        else:
            if stage_overlap:
                if len(stage_overlap) >= 2:
                    score_total += 4
                else:
                    score_total += 3
            elif len(activity["stages"]) >= 3:
                score_total += 1
        
        # Skill relevance (0-4 points)
        skill_overlap = set(priority_skills) & set(activity["skills"])
        skill_points = min(len(skill_overlap), 4)
        score_total += skill_points
        
        # Bonus for purposeful_writing for conventional writers
        if is_conventional and "purposeful_writing" in activity["skills"]:
            score_total += 2
        
        scored_activities.append({
            "activity": activity,
            "score": score_total
        })
    
    # Sort by score descending
    scored_activities.sort(key=lambda x: x["score"], reverse=True)
    
    # For conventional writers, ensure we get activities tagged "conventional"
    if is_conventional:
        conventional_activities = [a for a in scored_activities if "conventional" in a["activity"]["stages"]]
        other_activities = [a for a in scored_activities if "conventional" not in a["activity"]["stages"]]
        
        random.shuffle(conventional_activities)
        random.shuffle(other_activities[:10])
        
        num_to_select = 5
        selected = conventional_activities[:num_to_select]
        
        if len(selected) < num_to_select:
            remaining = num_to_select - len(selected)
            selected.extend(other_activities[:remaining])
    else:
        pool_size = min(20, len(scored_activities))
        top_pool = scored_activities[:pool_size]
        random.shuffle(top_pool)
        
        num_to_select = 4
        selected = top_pool[:num_to_select]
    
    # Format for return
    results = []
    for item in selected:
        activity = item["activity"].copy()
        activity["description"] = activity["description"].replace("{child_name}", child_name)
        activity["title"] = activity["title"].replace("{child_name}", child_name)
        results.append(activity)
    
    return results



def get_developmental_milestones(stage: str, age_months: int, child_name: str) -> dict:
    """Return developmental milestones to look for based on current stage and age.
    
    Provides parents with concrete signs of progress to watch for.
    """
    
    if stage == "ALREADY WRITING":
        return {
            "intro": f"Since {child_name} is already writing real letters, here are signs of continued progress:",
            "signs": [
                f"{child_name} writing their full name with increasing accuracy",
                "Attempting to write simple words independently",
                "Asking how to spell words",
                "Writing letters or notes to family members (even if not all spellings are correct)",
                "Showing pride in their writing and wanting to share it"
            ],
            "note": f"Since {child_name} is already writing actual letters, this assessment's score is not really meaningful for them. What matters now is encouraging their love of writing and building on their strong foundation."
        }
    elif stage == "STRONG START":
        return {
            "intro": f"Since {child_name} is already showing good differentiation between writing and drawing, here are signs of continued development to look for:",
            "signs": [
                f"Letter-like shapes appearing in {child_name}'s 'writing' - these may not be real letters yet, but shapes that look like they could be",
                "Asking 'What does this say?' about print in books or on signs",
                f"{child_name} 'reading' to toys or family members by making up stories while looking at books",
                "Attempting to write their name or the first letter of their name",
                "Drawing people with more detail (faces with features, bodies with limbs)"
            ],
            "note": "These signs typically emerge over the coming months. Every child develops at their own pace."
        }
    elif stage == "BEGINNING EXPLORER":
        return {
            "intro": f"As {child_name}'s understanding develops, here are signs of progress to look for:",
            "signs": [
                f"{child_name} making their 'writing' look noticeably different from their drawings - smaller, with different colours or mark types",
                "Showing interest in letters, especially letters in their own name",
                "Asking what words say in books or on packaging",
                "'Pretend writing' that looks like rows of marks (even if not real letters)",
                "Using darker colours for 'writing' and brighter colours for pictures"
            ],
            "note": "With the activities suggested above, you may see these signs emerging over the next few months."
        }
    else:  # EARLY DAYS
        if age_months < 32:
            return {
                "intro": f"At {child_name}'s age, here are positive signs of development to look for:",
                "signs": [
                    f"{child_name} enjoying making marks with different tools (crayons, chalk, paint)",
                    "Making marks intentionally rather than randomly",
                    "Showing you their marks and wanting a response",
                    "Beginning to hold crayons with more control",
                    "Making circular scribbles (an important pre-writing skill)"
                ],
                "note": "The understanding that writing and drawing are different typically begins to emerge from around 2 years and 8 months. Right now, enjoying mark-making is the most important thing."
            }
        else:
            return {
                "intro": f"As {child_name} develops, here are signs that differentiation between writing and drawing is beginning to emerge:",
                "signs": [
                    f"Any difference at all between how {child_name} approaches 'writing' versus 'drawing' - even small differences are progress",
                    "Interest in books, especially pointing at pictures and words",
                    "Noticing print in the environment (signs, labels, packaging)",
                    "Making marks that are more controlled and deliberate",
                    "'Pretend reading' - holding a book and making up a story"
                ],
                "note": "These signs may emerge gradually over the coming months. The activities suggested above will support this development."
            }


# =============================================================================
# AGE-ADJUSTED SCORING
# =============================================================================

def get_age_adjusted_bands(age_months: int) -> dict:
    """Return scoring bands calibrated for 24-point maximum score."""
    if age_months <= 30:
        # Youngest children (24-30 months): most lenient thresholds
        return {
            "strong_start": {"min": 12, "min_percent": 50},
            "beginning_explorer": {"min": 6, "max": 11, "min_percent": 25, "max_percent": 49},
            "early_days": {"max": 5, "max_percent": 24}
        }
    elif age_months <= 42:
        # Middle age group (31-42 months): moderate thresholds
        return {
            "strong_start": {"min": 15, "min_percent": 62},
            "beginning_explorer": {"min": 9, "max": 14, "min_percent": 37, "max_percent": 61},
            "early_days": {"max": 8, "max_percent": 36}
        }
    else:
        # Oldest children (43-48 months): strictest thresholds
        return {
            "strong_start": {"min": 18, "min_percent": 75},
            "beginning_explorer": {"min": 12, "max": 17, "min_percent": 50, "max_percent": 74},
            "early_days": {"max": 11, "max_percent": 49}
        }


def determine_stage(score: int, age_months: int, child_name: str = "Your child") -> dict:
    bands = get_age_adjusted_bands(age_months)
    if score >= bands["strong_start"]["min"]:
        return {
            "stage": "STRONG START", 
            "description": f"{child_name} shows clear understanding that writing and drawing are different", 
            "detail": f"{child_name} is making distinct marks for writing versus drawing. This is excellent progress for their age."
        }
    elif score >= bands["beginning_explorer"]["min"]:
        return {
            "stage": "BEGINNING EXPLORER", 
            "description": f"{child_name} is starting to understand that writing and drawing are different", 
            "detail": f"{child_name} is beginning to show some differences between writing and drawing."
        }
    else:
        return {
            "stage": "EARLY DAYS", 
            "description": f"{child_name} is still exploring mark-making", 
            "detail": f"{child_name} is enjoying making marks on paper. The understanding that writing and drawing are different typically emerges over the coming months."
        }


def determine_stage_with_writing_stage(total_score: int, max_score: int, age_months: int, writing_stage: str, child_name: str = "Your child", blind_result: dict = None) -> dict:
    """
    Determine developmental stage using floor logic.
    
    FLOOR LOGIC: writing_stage sets MINIMUM stage
    - CONVENTIONAL → minimum STRONG START (but check short name cap)
    - EMERGING → minimum STRONG START  
    - LETTER_LIKE → minimum BEGINNING EXPLORER
    - SCRIBBLES/DRAWING → use score-based determination
    
    Differentiation score can BOOST stage up, never pull down.
    """
    
    # Get score-based stage first
    score_based = determine_stage(total_score, age_months, child_name)
    score_stage = score_based["stage"]
    
    # Define stage hierarchy (higher index = more advanced)
    stage_order = ["EARLY DAYS", "BEGINNING EXPLORER", "STRONG START", "ALREADY WRITING"]
    
    def stage_index(stage_name):
        try:
            return stage_order.index(stage_name)
        except ValueError:
            return 0
    
    # Determine floor based on writing_stage
    writing_stage_upper = writing_stage.upper() if writing_stage else ""
    
    if writing_stage_upper == "CONVENTIONAL":
        # Check for short name cap
        if blind_result:
            name_letter_count = blind_result.get('name_letter_count', 0)
            sun_readable = blind_result.get('sun_readable', False)
            
            if name_letter_count <= 3 and not sun_readable:
                # Short name without sun - cap at STRONG START
                floor_stage = "STRONG START"
            else:
                # Full CONVENTIONAL → ALREADY WRITING
                floor_stage = "ALREADY WRITING"
        else:
            floor_stage = "ALREADY WRITING"
    elif writing_stage_upper == "EMERGING":
        floor_stage = "STRONG START"
    elif writing_stage_upper == "LETTER_LIKE":
        floor_stage = "BEGINNING EXPLORER"
    else:
        # SCRIBBLES, DRAWING, or unknown - no floor, use score
        floor_stage = "EARLY DAYS"
    
    # Apply floor logic: take the HIGHER of score-based or floor
    if stage_index(floor_stage) > stage_index(score_stage):
        final_stage = floor_stage
        boost_applied = True
    else:
        final_stage = score_stage
        boost_applied = False
    
    # Build response based on final stage
    if final_stage == "ALREADY WRITING":
        return {
            "stage": "ALREADY WRITING",
            "description": f"{child_name} is writing real words!",
            "detail": f"{child_name} can write recognisable words - this is excellent progress that puts them ahead of typical development for their age.",
            "short_name_capped": False
        }
    elif final_stage == "STRONG START":
        short_name_capped = (writing_stage_upper == "CONVENTIONAL" and 
                           blind_result and 
                           blind_result.get('name_letter_count', 0) <= 3 and 
                           not blind_result.get('sun_readable', False))
        return {
            "stage": "STRONG START",
            "description": f"{child_name} shows clear understanding that writing and drawing are different",
            "detail": f"{child_name} is making distinct marks for writing versus drawing. This is excellent progress for their age.",
            "short_name_capped": short_name_capped
        }
    elif final_stage == "BEGINNING EXPLORER":
        return {
            "stage": "BEGINNING EXPLORER",
            "description": f"{child_name} is starting to understand that writing and drawing are different",
            "detail": f"{child_name} is beginning to show some differences between writing and drawing.",
            "short_name_capped": False
        }
    else:
        return {
            "stage": "EARLY DAYS",
            "description": f"{child_name} is still exploring mark-making",
            "detail": f"{child_name} is enjoying making marks on paper. The understanding that writing and drawing are different typically emerges over the coming months.",
            "short_name_capped": False
        }


def interpret_verbal_behaviour(questionnaire: dict, score: int, stage: str, child_name: str = "Your child") -> dict:
    interpretations = []
    patterns = []
    general_behaviour = questionnaire.get("general_behaviour", [])
    writing_comments = questionnaire.get("writing_comments", "").strip()
    drawing_comments = questionnaire.get("drawing_comments", "").strip()
    
    if "said_writing_hard" in general_behaviour:
        patterns.append("difficulty_writing")
        interpretations.append(f"{child_name} recognised that writing is challenging - this shows awareness that writing is different from drawing.")
    if "confident_no_comment" in general_behaviour:
        patterns.append("confident")
        interpretations.append(f"{child_name} approached both tasks confidently.")
    if "needed_encouragement" in general_behaviour:
        patterns.append("needed_encouragement")
        interpretations.append(f"{child_name} needed some encouragement, which is completely normal at this age.")
    if "enjoyed_it" in general_behaviour:
        patterns.append("enjoyed")
        interpretations.append(f"{child_name} seemed to enjoy the activities!")
    if "said_drawing_hard" in general_behaviour:
        patterns.append("difficulty_drawing")
        interpretations.append(f"{child_name} found drawing challenging too - they may still be developing confidence with mark-making.")
    if "didnt_say_much" in general_behaviour:
        patterns.append("quiet")
        interpretations.append(f"{child_name} was quietly focused on the tasks.")
    
    if writing_comments:
        skip_phrases = ["nothing", "n/a", "na", "none", "no", "did not say", "didnt say", "-", ""]
        if writing_comments.lower().strip() not in skip_phrases and len(writing_comments.strip()) > 3:
            patterns.append("writing_comment_provided")
            interpretations.append(f"While writing, you noted: \"{writing_comments}\"")
    
    if drawing_comments:
        skip_phrases = ["nothing", "n/a", "na", "none", "no", "did not say", "didnt say", "-", ""]
        if drawing_comments.lower().strip() not in skip_phrases and len(drawing_comments.strip()) > 3:
            patterns.append("drawing_comment_provided")
            interpretations.append(f"While drawing, you noted: \"{drawing_comments}\"")
    
    return {"patterns": patterns, "interpretations": interpretations}

def get_pair_comparison_interpretation(visual_scores: dict) -> str:
    pair1 = visual_scores.get("pair1_subtotal", 0)
    pair2 = visual_scores.get("pair2_subtotal", 0)
    
    # Metacognitive awareness: strong name differentiation but drew sun instead
    if pair1 >= 9 and pair2 <= 3:
        return "Your child differentiated well for their name but not for 'sun.' If they said they didn't know how to write it, that's actually positive—they understand writing should look different but need more letter knowledge."
    
    pair1_pct = (pair1 / 10) * 100
    pair2_pct = (pair2 / 11) * 100
    diff = abs(pair1_pct - pair2_pct)
    if diff < 15:
        return "Your child shows consistent differentiation across both pairs of tasks."
    elif pair1_pct > pair2_pct:
        return "Your child shows stronger differentiation with their name and self-portrait."
    else:
        return "Your child shows stronger differentiation with the sun tasks."


def get_conventional_writing_feedback(child_name: str, writing_stage_reasoning: str) -> dict:
    """Return feedback for children already writing actual letters.
    
    This assessment is designed for children who have not yet learned to write.
    If a child is already writing real letters, they have moved beyond this stage.
    """
    return {
        "stage": "ALREADY WRITING",
        "description": f"{child_name} is already writing real letters!",
        "detail": f"{child_name} has progressed beyond the early mark-making stage this assessment measures. They are already forming recognisable letters, which is wonderful! This assessment is designed for children who are still learning that writing and drawing are different - {child_name} has already mastered this concept.",
        "is_conventional": True,
        "recommendation": f"Since {child_name} is already writing, you might want to explore activities that build on their existing skills: practising letter formation, learning to write their full name, or exploring simple words. Well done!",
        "writing_stage_reasoning": writing_stage_reasoning
    }


def get_emerging_writing_feedback(child_name: str, stage_info: dict, writing_stage_reasoning: str) -> dict:
    """Enhance feedback for children showing emerging letter formation."""
    enhanced = stage_info.copy()
    enhanced["writing_development"] = f"{child_name} is starting to form some real letters! This is an exciting development."
    enhanced["writing_stage_reasoning"] = writing_stage_reasoning
    return enhanced


# =============================================================================
# JSON PARSING HELPER
# =============================================================================

def parse_json_response(ai_response: str) -> dict:
    if not ai_response:
        return None
    
    strategies = [
        lambda s: json.loads(s.strip()),
        lambda s: json.loads(s.replace("```json", "").replace("```", "").strip()),
        lambda s: json.loads(s[s.find("{"):s.rfind("}")+1]),
        lambda s: json.loads(extract_json_object(s)),
    ]
    
    for i, strategy in enumerate(strategies):
        try:
            result = strategy(ai_response)
            if isinstance(result, dict) and 'pair1_subtotal' in result or 'pair2_subtotal' in result:
                logging.info(f'JSON parsed successfully with strategy {i+1}')
                return result
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logging.debug(f'Strategy {i+1} failed: {str(e)[:100]}')
            continue
    
    logging.error(f'All JSON parsing strategies failed. Response preview: {ai_response[:300]}')
    return None


def extract_json_object(text: str) -> str:
    start = text.find('{')
    if start == -1:
        return text
    
    depth = 0
    for i, char in enumerate(text[start:], start):
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    
    return text[start:text.rfind('}')+1]


# =============================================================================
# TREIMAN RUBRIC
# =============================================================================

TREIMAN_RUBRIC = """
You are an expert scorer for early childhood writing development assessments based on Treiman & Yin (2011).

TARGET AGE: 24-42 months (2-3.5 years)

You will receive images from a young child. Score them according to the rubric below.

CRITICAL ACCURACY RULES - READ CAREFULLY:

BEFORE YOU BEGIN: Look at each image carefully. Describe ONLY what is literally visible. Do not embellish, improve, or interpret generously.

1. ONLY describe what you ACTUALLY SEE in each image - do not assume or hallucinate
2. If an image shows ONLY ONE COLOUR, state that single colour only
3. If WRITING and DRAWING images look IDENTICAL or very similar:
   - Score ALL differentiation categories as 0
   - This is important developmental information - the child has not yet learned to differentiate
   - State clearly in observations that the images are identical/similar
4. Be precise about colours: "yellow marks" not "yellow and blue" if only yellow is visible
5. Do NOT give credit for differentiation that does not exist in the images

LETTER QUALITY ACCURACY - RESEARCH-BASED FRAMEWORK:

RADICAL HONESTY: Only call a letter "recognisable" if you can DEFINITELY identify it without guessing. If uncertain, call it "letter-like mark". One clear letter is better than three unclear ones.

Example - WRONG: "recognisable letters 'O', 'b' and 'i'" (if only O is clearly visible)
Example - RIGHT: "one recognisable letter 'O' with letter-like marks"

WHICH LETTERS TO IDENTIFY:
Research shows children typically write the first letter of their name before others (Bloodgood, 1999; Puranik & Lonigan, 2011), but assess what you actually see. Always specify which letters by name. Count only letters you can DEFINITELY identify, regardless of position.

AGES 2-4 (Target age): Typical writing shows 1-3 recognisable letters (often just first letter of name), wobbly formation, inconsistent sizing, poor spacing, unsteady lines, reversals (normal). Letter-like shapes without clear identity are common and normal. Use language: "wobbly", "unsteady lines", "inconsistent sizing", "letter-like marks". Avoid: "developing", "improving".

AGES 5-7: Most name letters recognisable, minimal reversals by age 7, more consistent sizing and spacing than ages 2-4, mix of clear and unclear letters. Use: "most letters recognisable", "straighter lines", "more even spacing", "legible".

AGES 7-8+: All letters clearly identifiable, consistent formation, uniform sizing/spacing, proper alignment, no reversals, steady smooth lines, organised appearance. Use: "consistent formation", "uniform sizing", "steady lines", "all letters clearly recognisable".

CRITICAL RULES:
1. Assess only visible characteristics - no speed/fluency/motor control claims
2. For ages 2-4: reversals, wobbly lines, unclear marks are NORMAL
3. Never use "clearly formed", "neat", "well-formed" for typical 2-4 year old writing
4. Be radically honest - one clear letter is better than three unclear ones
5. One recognisable letter is an achievement for ages 2-3

VERIFY: Counted only definite letters? Specified which letters? Used only observable characteristics? Honest about quality?

SOURCES: Illinois Early Learning Project, Learning Without Tears, OFSTED Research Review, North Shore Pediatric Therapy, Growing Hands-On Kids, Bloodgood (1999), Both-de Vries & Bus (2010), Puranik & Lonigan (2011, 2012), Justice et al. (2006)


FAVOURITE COLOUR DETECTION - MANDATORY:
YOU MUST ALWAYS check for and report dominant colours in the favourite_colour_detected field.

DETECTION RULES:
1. Look at ALL samples and identify which colour(s) appear most frequently
2. If the SAME colour appears in 2+ different samples → set favourite_colour_detected to that colour name
3. If multiple colours appear equally → choose the one that appears in the most samples
4. ALWAYS set favourite_colour_detected to a specific colour name OR "none" - NEVER leave it empty

COLOUR NAMES TO USE:
- Standard colours: "black", "blue", "brown", "green", "orange", "pink", "purple", "red", "yellow"
- Custom colours the parent might mention: "turquoise", "gold", "navy", "silver", "grey", "teal", "maroon", etc.

EXAMPLES:
- Name writing uses blue + Self portrait uses blue → favourite_colour_detected = "blue"
- Name writing uses black + Self portrait uses red/green/yellow → favourite_colour_detected = "none" 
- Sun writing uses blue + Sun drawing uses blue → favourite_colour_detected = "blue"
- All 4 samples use blue → favourite_colour_detected = "blue"

Parent stated favourite colour: {favourite_colour_stated}
If this colour appears in the samples, note it. If a DIFFERENT colour dominates, report what you actually see.

PAIR 2 SPECIAL CASE - BOTH SAMPLES ARE DRAWINGS:
- If BOTH the sun writing and sun drawing samples are DRAWINGS (no attempt at letters or letter-like marks):
  - This means the child drew for both tasks because they do not know how to spell "sun"
  - Set pair2_both_are_drawings to true
  - Score Pair 2 normally but note in observations: "Both samples were drawings"
  - Explain: "The child drew for both because they do not yet know how to write the word sun, which is age-appropriate"

SCORING RULE: If the writing and drawing samples look the same (same colours, same shapes, same size, same style), ALL scores for that pair should be 0 or very low. The child showing NO differentiation is a valid and important finding.

KEY DIFFERENCES (from Treiman's research) - only score highly if these ARE present:
- WRITING: smaller, darker, more angular, sparser
- DRAWING: larger, more colourful, more curved, denser

WRITING STAGE CLASSIFICATION - CRITICAL:
You MUST classify the developmental stage of the WRITING samples (not drawing). Look at the name_writing and sun_writing images and classify:

- SCRIBBLES: Random marks with no letter-like qualities. Circular scribbles, random lines, no attempt at letter forms.
- LETTER_LIKE: Real letter shapes that do NOT represent the target word. Letters appear random or borrowed from the child's name. Example: Child writes "GMLEF" for "light" — real letters, but random. Example: Child writes letters from their own name for every word.
- EMERGING: 1-2 letters that REPRESENT the actual target word, often the first letter. Example: Clear "O" for "Obi" — the O represents their name. Example: Clear "S" for "sun" — the S represents the word.
- CONVENTIONAL: MOST or ALL letters are clearly recognisable. An adult could read the entire word without being told what it says.

LETTER_LIKE vs EMERGING - KEY DISTINCTION:
- LETTER_LIKE: Real letters but RANDOM (do not match the target word)
- EMERGING: Real letters that MATCH the target word (even if only 1-2 letters)

FIRST LETTER RESEARCH (Bloodgood, 1999; Puranik & Lonigan, 2011):
Children typically write the first letter of their name correctly before other letters. If only ONE letter is clear and it is the FIRST letter of the name, this is EMERGING — a normal developmental milestone for ages 3-4.

AGE CONTEXT (Treiman & Yin, 2011):
At ages 2-3, children rarely produce correct letters. If you classify a 2-3 year old as CONVENTIONAL, double-check your letter identification is strict.


CRITICAL DISTINCTION - READ CAREFULLY:
- If only 1-2 letters are readable → EMERGING (even if those letters are perfect)
- If most/all letters are readable → CONVENTIONAL

CRITICAL - PREVENT CONFIRMATION BIAS:

You are told the child's name, but DO NOT use it to interpret what you see. You must identify letters as if you DO NOT KNOW the child's name.

WRONG approach: "Child is Olivia, I see marks, these must be O-l-i-v-a"
RIGHT approach: "I see one clear circular letter 'O'. The remaining marks are unclear and I cannot confidently identify them as specific letters."

TEST YOURSELF:
- Cover the child's name mentally
- Look at ONLY the marks on the page
- Which letters can you DEFINITELY identify without knowing the name?
- If you need the name to "see" the letters, they are NOT clearly readable

COMMON CONFIRMATION BIAS ERRORS:
- "I see O, l, i" when only "O" is actually clear (because you know the name is Oli/Olivia/Obi)
- "I see S, a, m" when only "S" is clear (because you know the name is Sam)
- Counting wobbly marks as letters because they COULD match the expected name
- Being generous because you want the letters to spell the name

BE STRICT:
- Only count letters you could identify WITHOUT knowing the name
- One clear letter + unclear marks = EMERGING, not CONVENTIONAL
- If you are unsure about a letter, it does NOT count as recognisable
- "Possibly an 'l'" means it is NOT clearly recognisable

LETTER COUNT CHECK:
After identifying letters, ask: "Would a stranger who doesn't know this child's name read these same letters?"
- If YES for most/all letters → CONVENTIONAL
- If YES for only 1-2 letters → EMERGING
- If NO for all letters → LETTER_LIKE or SCRIBBLES


- When in doubt, choose EMERGING over CONVENTIONAL
- Short names (2-3 letters) need ALL letters clear for CONVENTIONAL
- Longer names (4+ letters) need MOST letters clear for CONVENTIONAL

EXAMPLE CLASSIFICATIONS:

EMERGING examples (1-2 clear letters only):
- "Obi" where only "O" is clearly a letter, "b" and "i" are wobbly marks → EMERGING
- "Sam" where "S" is clear but "am" are scribbles → EMERGING
- "Maya" where "M" and "a" are recognisable but "ya" are unclear → EMERGING
- "Tom" where "T" is clear but "om" are joined/unclear → EMERGING
- Any name where you can only confidently identify 1-2 letters → EMERGING
- First letter perfect, rest are attempts → EMERGING
- Some letters reversed or backwards but recognisable → EMERGING

CONVENTIONAL examples (most/all letters readable):
- "Yvette" where all 6 letters can be identified → CONVENTIONAL
- "Sam" where S, a, and m are all clearly readable → CONVENTIONAL
- "Obi" where O, b, and i are all identifiable (even if wobbly) → CONVENTIONAL
- "sun" where s, u, and n are all readable → CONVENTIONAL
- "Maya" where all 4 letters can be read → CONVENTIONAL
- Letters may be wobbly/uneven but an adult can read the whole word → CONVENTIONAL

NOT CONVENTIONAL (common mistakes to avoid):
- Stick figure with circular head is NOT the letter "O"
- Drawing that happens to contain circular shapes is NOT letter writing
- Marks that COULD be letters if you squint are NOT conventional writing
- One perfect letter + scribbles = EMERGING, not CONVENTIONAL
- Being generous about unclear letters = WRONG, be strict

ASK YOURSELF:
1. Could a stranger read this word without being told what it says?
2. How many letters can I DEFINITELY identify?
3. Am I being generous or strict? (Be strict)

If a stranger could not read the word → NOT CONVENTIONAL
If you can only identify 1-2 letters with certainty → EMERGING
If most/all letters are readable by anyone → CONVENTIONAL

THIS IS CRITICAL: If you see real letter writing (real, readable letters like proper alphabet letters), you MUST classify it as CONVENTIONAL. This indicates the child is BEYOND the target developmental stage for this assessment.

Signs of real letter writing:
- Clearly readable letters (A, B, C, etc.)
- Proper letter formation
- Could be read by any adult
- Looks like actual handwriting, not scribbles or attempts

SCORING RUBRIC (24 points total for full assessment):

PAIR 1: NAME vs SELF-PORTRAIT (10 points)
1. SIZE DIFFERENCE (0-3): 3=name significantly smaller, 2=moderately smaller, 1=slightly smaller, 0=same size or larger
2. COLOUR DIFFERENTIATION (0-2): 2=name uses dark colour (black/grey/pencil) while portrait uses colours, 1=both use same colours, 0=name uses bright colours while portrait uses dark (reverse of expected)
3. ANGULARITY (0-2): 2=clear difference in mark types, 1=moderate, 0=same mark types
4. DENSITY (0-2): 2=name clearly sparser, 1=moderate, 0=same density
5. SHAPE FEATURES (0-1): 1=portrait has recognisable features + name has letter-like shapes, 0=similar appearance

PAIR 2: SUN WRITING vs SUN DRAWING (11 points)
6. SIZE DIFFERENCE (0-3): Same as above - 0 if same size
7. COLOUR + OBJECT-APPROPRIATE (0-3): 3=writing dark + drawing yellow/orange, 2=writing dark + drawing coloured, 1=slight difference, 0=SAME colours in both (score 0 if both are yellow)
8. ANGULARITY (0-2): Same as above - 0 if same mark types
9. DENSITY (0-2): Same as above - 0 if same density
10. SHAPE FEATURES (0-1): 1=drawing is sun-shaped + writing is letter-like, 0=both look similar

CROSS-PAIR (3 points) - Only if both pairs provided
11. WRITING CONSISTENCY (0-2): 2=both writing samples similar style, 1=moderate, 0=different
12. DRAWING VARIETY (0-1): 1=portrait and sun look appropriately different, 0=similar

OBSERVATIONS RULES:
- Describe EXACTLY and ONLY what you see in each image
- Start each observation by stating what the child was asked to do: "The child produced..."
- Do NOT use "ink" to describe marks. Use: "pencil", "pen", "crayon", or just the colour (e.g., "in black", "using black pencil")
- DOTS AND SPECKS ARE NOT LETTERS: Small scattered dots are SCRIBBLES, not letter-like marks. Letter-like marks must have clear line structure.
- If images are identical, say "Identical to [other image]" or "Same as [other image]"
- NEVER LIST SPECIFIC LETTERS BY NAME in observations. Instead describe:
  - "recognisable letter forms" or "clearly formed letters"
  - "three distinct letters" or "several letter shapes"
  - Example GOOD: "The child produced recognisable letter forms in black pencil"
  - Example BAD: "The child wrote O, A, and b"
- The parent can see the uploaded image - your job is to assess development level, NOT to transcribe the writing
- Focus on: colour used, size, mark quality (wobbly/steady), whether letters are recognisable as a group

MANDATORY LETTER COUNT:
Before classifying writing_stage, you MUST count letters honestly:

1. letters_identified = letters you are 100% CERTAIN about
2. letters_uncertain = letters you THINK might be there but are not sure
3. Only count CERTAIN letters when deciding the classification

RULE:
- Certain letters < half the name → EMERGING (maximum)
- Certain letters >= half the name → Could be CONVENTIONAL

EXAMPLE - Name is "Obi" (3 letters):
- You see: clear "O", wobbly marks for "b" and "i"
- letters_identified: ["O"]
- letters_uncertain: ["b", "i"]
- Certain count: 1 out of 3 = 33%
- 33% is less than half → EMERGING (not CONVENTIONAL)

EXAMPLE - Name is "Yvette" (6 letters):
- You see: all 6 letters clearly readable
- letters_identified: ["Y", "v", "e", "t", "t", "e"]
- letters_uncertain: []
- Certain count: 6 out of 6 = 100%
- 100% is more than half → CONVENTIONAL

CRITICAL: Respond with ONLY a valid JSON object. No explanations, no markdown, no text before or after the JSON. Start your response with { and end with }.

JSON FORMAT:
{"writing_stage":"SCRIBBLES|LETTER_LIKE|EMERGING|CONVENTIONAL","writing_stage_reasoning":"Explain what you see in the writing samples that led to this classification","letters_identified":["list only letters you are 100% certain about"],"letters_uncertain":["list letters you think might be there but unsure"],"pair1_scores":{"size_difference":{"score":0,"max":3,"reasoning":""},"colour_differentiation":{"score":0,"max":2,"reasoning":""},"angularity":{"score":0,"max":2,"reasoning":""},"density":{"score":0,"max":2,"reasoning":""},"shape_features":{"score":0,"max":1,"reasoning":""}},"pair1_subtotal":0,"pair2_scores":{"size_difference":{"score":0,"max":3,"reasoning":""},"colour_object_appropriate":{"score":0,"max":3,"reasoning":""},"angularity":{"score":0,"max":2,"reasoning":""},"density":{"score":0,"max":2,"reasoning":""},"shape_features":{"score":0,"max":1,"reasoning":""}},"pair2_subtotal":0,"pair2_both_are_drawings":false,"cross_pair_scores":{"writing_consistency":{"score":0,"max":2,"reasoning":""},"drawing_variety":{"score":0,"max":1,"reasoning":""}},"cross_pair_subtotal":0,"total_score":0,"max_score":24,"observations":{"name_writing":"","self_portrait":"","sun_writing":"","sun_drawing":""},"favourite_colour_detected":"none or colour name","strongest_evidence":"","areas_for_development":""}
For PARTIAL assessments (only one pair), set the missing pair's scores to null and subtotal to 0.
"""


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route(route="health")
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Health check endpoint called.')
    return func.HttpResponse(
        json.dumps({
            "status": "healthy",
            "message": "Early Writing Starter API is running!",
            "version": "7.7.0",
            "max_score": 24,
            "features": ["writing_stage_detection", "conventional_writer_handling", "email_tracking"],
            "services": {
                "openai": "available" if OPENAI_AVAILABLE else "not installed",
                "storage": "available" if STORAGE_AVAILABLE else "not installed",
                "cosmos_db": "available" if COSMOS_AVAILABLE else "not installed",
                "pdf_generation": "available" if REPORTLAB_AVAILABLE else "not installed",
                "email": "available" if EMAIL_AVAILABLE else "not installed"
            },
            "partial_assessments": "supported"
        }),
        mimetype="application/json",
        status_code=200
    )


# =============================================================================
# SESSION TOKEN MANAGEMENT (for "Do It Later" feature)
# =============================================================================

def get_sessions_container():
    conn_str = os.environ.get("COSMOS_DB_CONNECTION_STRING")
    if not conn_str:
        return None
    client = CosmosClient.from_connection_string(conn_str)
    database = client.get_database_client("AssessmentDB")
    container = database.get_container_client("sessions")
    return container


@app.route(route="store_session", methods=["POST"])
def store_session(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Store session endpoint called.')
    
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Database not available"}), mimetype="application/json", status_code=503)
    
    try:
        req_body = req.get_json()
        session_token = req_body.get('session_token')
        
        if not session_token:
            return func.HttpResponse(json.dumps({"error": "session_token required"}), mimetype="application/json", status_code=400)
        
        container = get_sessions_container()
        if not container:
            return func.HttpResponse(json.dumps({"error": "Sessions container not available"}), mimetype="application/json", status_code=503)
        
        expiry_date = datetime.utcnow() + timedelta(days=30)
        
        session_data = {
            "id": session_token,
            "session_token": session_token,
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": expiry_date.isoformat(),
            "used": False,
            "is_test": req_body.get('is_test', False),
            "ttl": 30 * 24 * 60 * 60
        }
        
        container.upsert_item(body=session_data)
        logging.info(f'Session stored: {session_token}')
        
        return func.HttpResponse(json.dumps({"success": True}), mimetype="application/json", status_code=200)
        
    except Exception as e:
        logging.error(f'Error storing session: {str(e)}')
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


@app.route(route="verify_session", methods=["GET"])
def verify_session(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Verify session endpoint called.')
    
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"valid": False, "error": "Database not available"}), mimetype="application/json", status_code=200)
    
    try:
        token = req.params.get('token')
        
        if not token:
            return func.HttpResponse(json.dumps({"valid": False, "error": "token required"}), mimetype="application/json", status_code=200)
        
        container = get_sessions_container()
        if not container:
            return func.HttpResponse(json.dumps({"valid": False, "error": "Sessions container not available"}), mimetype="application/json", status_code=200)
        
        try:
            session = container.read_item(item=token, partition_key=token)
            
            expires_at = datetime.fromisoformat(session.get('expires_at', '2000-01-01'))
            if datetime.utcnow() > expires_at:
                return func.HttpResponse(json.dumps({"valid": False, "error": "Session expired"}), mimetype="application/json", status_code=200)
            
            return func.HttpResponse(json.dumps({"valid": True}), mimetype="application/json", status_code=200)
            
        except exceptions.CosmosResourceNotFoundError:
            return func.HttpResponse(json.dumps({"valid": False, "error": "Session not found"}), mimetype="application/json", status_code=200)
        
    except Exception as e:
        logging.error(f'Error verifying session: {str(e)}')
        return func.HttpResponse(json.dumps({"valid": False, "error": str(e)}), mimetype="application/json", status_code=200)


@app.route(route="send_access_link", methods=["POST"])
def send_access_link(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Send access link endpoint called.')
    
    if not EMAIL_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Email not available"}), mimetype="application/json", status_code=503)
    
    try:
        req_body = req.get_json()
        recipient_email = req_body.get('recipient_email')
        access_link = req_body.get('access_link')
        
        if not recipient_email or not access_link:
            return func.HttpResponse(json.dumps({"error": "recipient_email and access_link required"}), mimetype="application/json", status_code=400)
        
        email_client = get_email_client()
        if not email_client:
            return func.HttpResponse(json.dumps({"error": "Email client not available"}), mimetype="application/json", status_code=503)
        
        subject = "Your Early Writing Starter Link"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h1 style="color: #1B75BC;">Your Early Writing Starter</h1>
                <p>Thank you for your purchase!</p>
                <p>When you are ready to do the activity with your child, click the button below:</p>
                <p style="margin: 30px 0;">
                    <a href="{access_link}" style="background: #1B75BC; color: white; padding: 15px 30px; text-decoration: none; border-radius: 8px; font-weight: bold;">Start Activity</a>
                </p>
                <p>Or copy this link: <br/><a href="{access_link}">{access_link}</a></p>
                <p><strong>This link is valid for 30 days.</strong></p>
                <hr style="border: none; border-top: 1px solid #ccc; margin: 30px 0;" />
                <p style="font-size: 14px; color: #666;">
                    <strong>Before you start, make sure you have:</strong><br/>
                    • 10–15 minutes of quiet time<br/>
                    • 4 sheets of paper<br/>
                    • Crayons or coloured pencils<br/>
                    • A pencil or pen<br/>
                    • Your child ready to draw and write
                </p>
                <hr style="border: none; border-top: 1px solid #ccc; margin: 30px 0;" />
                <p style="font-size: 12px; color: #666;">
                    Questions? Contact us at <a href="mailto:contact@morehandwriting.co.uk">contact@morehandwriting.co.uk</a><br/><br/>
                    © More Handwriting | <a href="https://morehandwriting.co.uk">morehandwriting.co.uk</a>
                </p>
            </div>
        </body>
        </html>
        """
        
        message = {
            "senderAddress": "DoNotReply@morehandwriting.co.uk",
            "recipients": {"to": [{"address": recipient_email}]},
            "content": {"subject": subject, "html": html_content}
        }
        
        poller = email_client.begin_send(message)
        result = poller.result()
        
        return func.HttpResponse(json.dumps({"success": True, "message_id": result.get("id", "")}), mimetype="application/json", status_code=200)
        
    except Exception as e:
        logging.error(f'Error sending access link: {str(e)}')
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# UPLOAD IMAGES
# =============================================================================

@app.route(route="upload_images", methods=["POST"])
def upload_images(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Upload images endpoint called.')
    
    if not STORAGE_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
    
    try:
        req_body = req.get_json()
        images = req_body.get('images', {})

        # Get the parent's stated favourite colour
        questionnaire = req_body.get('questionnaire', {})
        favourite_colour_stated = questionnaire.get('favourite_colour', 'not specified')

        # Get the parent's stated favourite colour
        questionnaire = req_body.get('questionnaire', {})
        favourite_colour_stated = questionnaire.get('favourite_colour', 'not specified')
        pair1_complete = bool(images.get('name_writing') and images.get('self_portrait'))
        pair2_complete = bool(images.get('sun_writing') and images.get('sun_drawing'))
        
        if not pair1_complete and not pair2_complete:
            return func.HttpResponse(json.dumps({"error": "At least one complete pair required"}), mimetype="application/json", status_code=400)
        
        assessment_id = str(uuid.uuid4())
        image_urls = {}
        
        for image_name, image_base64 in images.items():
            if image_base64:
                image_data = base64.b64decode(image_base64)
                url = upload_image_to_blob(assessment_id, image_name, image_data)
                image_urls[image_name] = url
        
        return func.HttpResponse(json.dumps({"success": True, "assessment_id": assessment_id, "image_urls": image_urls}), mimetype="application/json", status_code=200)
    except Exception as e:
        logging.error(f'Error uploading images: {str(e)}')
        return func.HttpResponse(json.dumps({"error": "Failed to upload images", "details": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# SAVE/GET ASSESSMENT
# =============================================================================

@app.route(route="save_assessment", methods=["POST"])
def save_assessment(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Save assessment endpoint called.')
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Cosmos DB not available"}), mimetype="application/json", status_code=503)
    try:
        req_body = req.get_json()
        if not req_body.get('assessment_id'):
            return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
        result = save_assessment_to_db(req_body)
        return func.HttpResponse(json.dumps({"success": True, "assessment_id": result.get("assessment_id")}), mimetype="application/json", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


@app.route(route="get_assessment", methods=["GET"])
def get_assessment(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Get assessment endpoint called.')
    if not COSMOS_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Cosmos DB not available"}), mimetype="application/json", status_code=503)
    try:
        assessment_id = req.params.get('assessment_id')
        if not assessment_id:
            return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
        assessment = get_assessment_from_db(assessment_id)
        if not assessment:
            return func.HttpResponse(json.dumps({"error": "Not found"}), mimetype="application/json", status_code=404)
        return func.HttpResponse(json.dumps(assessment), mimetype="application/json", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# GENERATE/GET REPORT
# =============================================================================

@app.route(route="generate_report", methods=["POST"])
def generate_report(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Generate report endpoint called.')
    if not REPORTLAB_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "ReportLab not available"}), mimetype="application/json", status_code=503)
    try:
        req_body = req.get_json()
        assessment_data = req_body.get('assessment_data')
        if not assessment_data:
            assessment_id = req_body.get('assessment_id')
            if assessment_id and COSMOS_AVAILABLE:
                assessment_data = get_assessment_from_db(assessment_id)
        if not assessment_data:
            return func.HttpResponse(json.dumps({"error": "assessment_data or assessment_id required"}), mimetype="application/json", status_code=400)
        
        pdf_bytes = generate_assessment_pdf(assessment_data)
        
        if req_body.get('return_pdf', False):
            return func.HttpResponse(pdf_bytes, mimetype="application/pdf", status_code=200)
        
        if STORAGE_AVAILABLE:
            report_id = assessment_data.get('assessment_id', str(uuid.uuid4()))
            pdf_url = upload_pdf_to_blob(report_id, pdf_bytes)
            return func.HttpResponse(json.dumps({"success": True, "pdf_url": pdf_url}), mimetype="application/json", status_code=200)
        
        return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


@app.route(route="get_report", methods=["GET"])
def get_report(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Get report endpoint called.')
    if not STORAGE_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
    try:
        assessment_id = req.params.get('assessment_id')
        if not assessment_id:
            return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
        pdf_bytes = get_pdf_from_blob(assessment_id)
        return func.HttpResponse(pdf_bytes, mimetype="application/pdf", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# SEND EMAIL
# =============================================================================

@app.route(route="send_email", methods=["POST"])
def send_email(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Send email endpoint called.')
    if not EMAIL_AVAILABLE:
        return func.HttpResponse(json.dumps({"error": "Email not available"}), mimetype="application/json", status_code=503)
    try:
        req_body = req.get_json()
        recipient_email = req_body.get('recipient_email')
        if not recipient_email:
            return func.HttpResponse(json.dumps({"error": "recipient_email required"}), mimetype="application/json", status_code=400)
        
        assessment_data = req_body.get('assessment_data')
        if not assessment_data:
            assessment_id = req_body.get('assessment_id')
            if assessment_id and COSMOS_AVAILABLE:
                assessment_data = get_assessment_from_db(assessment_id)
        
        if not assessment_data:
            return func.HttpResponse(json.dumps({"error": "assessment_data or assessment_id required"}), mimetype="application/json", status_code=400)
        
        child_name = assessment_data.get('child', {}).get('name', 'Your Child')
        assessment_id = assessment_data.get('assessment_id', str(uuid.uuid4()))
        
        pdf_bytes = None
        if REPORTLAB_AVAILABLE:
            try:
                pdf_bytes = generate_assessment_pdf(assessment_data)
            except Exception as e:
                logging.warning(f'Failed to generate PDF: {str(e)}')
        
        result = send_assessment_email(recipient_email, child_name, assessment_id, pdf_bytes)
        return func.HttpResponse(json.dumps({"success": True, "email_status": result}), mimetype="application/json", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)

# =============================================================================
# BLIND STRANGER TEST - Detects conventional writers without confirmation bias
# =============================================================================

def blind_stranger_test(client, deployment_name, name_image: str = None, sun_image: str = None) -> dict:
    """
    Blind read test with fluent characteristic detection.
    
    CONVENTIONAL shortcut ONLY if:
    - Sun readable as "sun" (proves phonetic spelling), OR
    - Name has 4+ readable letters AND shows fluent characteristics
    
    RULES:
    1. ≤3 letter names CANNOT shortcut to CONVENTIONAL (not enough to assess fluency)
    2. Sun readable always triggers CONVENTIONAL (proves phonetic spelling)
    3. 4+ letter names need fluency check to shortcut without sun
    """
    content = [{"type": "text", "text": "Look at each image of a child's writing."}]
    
    labels = []
    if name_image:
        content.append({"type": "text", "text": "Image 1 (name writing):"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{name_image}"}})
        labels.append("name")
    if sun_image:
        content.append({"type": "text", "text": "Image 2 (sun writing):"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{sun_image}"}})
        labels.append("sun")
    
    if not labels:
        return {"is_established": False, "error": "No images provided"}
    
    try:
        response = client.chat.completions.create(
            model=deployment_name,
            messages=[{
                "role": "system",
                "content": """You assess whether a child's writing is readable AND shows fluent characteristics.

PART 1: READABILITY
Assess each sample honestly. Can you read what it says?

IMPORTANT - WOBBLY DOES NOT MEAN UNREADABLE:
- Young children's writing is often wobbly, uneven, or written with crayon - this does NOT mean unreadable
- If you can identify what the word says, it IS readable even if the strokes are imperfect
- Do NOT reject readable writing just because it looks childish or messy

SAY "I DON'T KNOW" ONLY if:
- The marks are random scribbles with no letter shapes
- You genuinely cannot tell what letters are intended
- The marks look like drawings, not writing attempts

STATE THE WORD if:
- You can identify what the word says (even if wobbly or messy)
- The letters are recognisable as specific letters
- A reasonable person would read it the same way

EXAMPLES:
- Wobbly "sun" in crayon → readable, state "sun"
- Messy "Bukunmi" with uneven letters → readable, state "Bukunmi"  
- Random scribbles with no letter shapes → "I DON'T KNOW"
- Circular marks that could be anything → "I DON'T KNOW"

PART 2: FLUENT CHARACTERISTICS (for name writing only)
If the name IS readable AND has 4 or more letters, assess whether it shows FLUENT writing characteristics.

FLUENT writing (typical of children age 5+ or advanced writers) shows ALL of:
- Consistent letter size (all letters similar height)
- Baseline adherence (letters sit on an invisible line)
- Smooth strokes (not wobbly or broken)
- Consistent pressure (all letters similar darkness)
- Proportional spacing (even gaps between letters)

NON-FLUENT writing (typical of ages 2-4) shows ANY of:
- Inconsistent letter size (some big, some small)
- No baseline (letters float at different heights)
- Wobbly strokes (unsteady lines)
- Uneven pressure (some letters darker or fainter than others)
- Irregular spacing (cramped or scattered)

IMPORTANT FOR SHORT NAMES (3 letters or fewer):
- You CANNOT assess fluency with only 2-3 letters
- Not enough data points to judge consistency
- Set fluent to false and reasoning to "Cannot assess fluency with X letters"

BE VERY STRICT about fluent characteristics:
- If even ONE feature is non-fluent, set fluent to false
- Most 2-4 year olds do NOT show fluent characteristics even if their letters are readable
- A readable but wobbly "OAb" is NOT fluent
- A perfectly formed "Yvette" with consistent size/baseline IS fluent

Also note the main colour used.

Reply ONLY with JSON:
{
  "image1_word": "word or I DON'T KNOW",
  "image1_colour": "colour",
  "image1_fluent": true or false,
  "image1_fluent_reasoning": "brief explanation of why fluent or not",
  "image2_word": "word or I DON'T KNOW", 
  "image2_colour": "colour"
}

If only one image, omit image2 fields.
Set image1_fluent to false if image1_word is "I DON'T KNOW"."""
            }, {
                "role": "user",
                "content": content
            }],
            max_tokens=300,
            temperature=0.0
        )
        
        result_text = response.choices[0].message.content
        logging.info(f"Blind stranger test raw: {result_text}")
        
        # Parse JSON response
        try:
            result = json.loads(result_text.replace("```json", "").replace("```", "").strip())
        except:
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(0))
            else:
                return {"is_established": False, "error": "Could not parse response"}
        
        name_word = result.get('image1_word')
        name_colour = result.get('image1_colour', '')
        name_fluent = result.get('image1_fluent', False)
        name_fluent_reasoning = result.get('image1_fluent_reasoning', '')
        sun_word = result.get('image2_word')
        sun_colour = result.get('image2_colour', '')
        
        # Check if readable
        not_readable_values = [
            "I DON'T KNOW", "I DONT KNOW", "IDK", "UNKNOWN", "UNCLEAR", 
            "NULL", "NONE", "N/A", "NA", "", "CAN'T READ", "CANT READ",
            "NOT SURE", "UNSURE", "ILLEGIBLE", "UNREADABLE"
        ]
        
        name_readable = name_word and str(name_word).upper().strip() not in not_readable_values
        
        # Check if sun is readable - strip punctuation and whitespace
        sun_word_clean = str(sun_word).upper().strip().rstrip('.,!?') if sun_word else ""
        sun_readable = sun_word_clean == "SUN"
        
        # Count letters in name
        name_letter_count = len(str(name_word).strip()) if name_readable else 0
        
        # Cannot assess fluency with ≤3 letters
        if name_letter_count <= 3:
            name_fluent = False
            name_fluent_reasoning = f"Cannot assess fluency with only {name_letter_count} letters"
        
        # Ensure fluent is False if not readable
        if not name_readable:
            name_fluent = False
            name_fluent_reasoning = "Not readable"
        
        # CONVENTIONAL shortcut logic
        is_established = (
            sun_readable or 
            (name_readable and name_letter_count >= 4 and name_fluent)
        )
        
        logging.info(f"Blind test decision: sun_readable={sun_readable}, name_readable={name_readable}, "
                    f"name_word={name_word}, name_letter_count={name_letter_count}, "
                    f"name_fluent={name_fluent}, is_established={is_established}")
        
        return {
            "is_established": is_established,
            "name_readable": name_readable,
            "name_word": name_word if name_readable else None,
            "name_letter_count": name_letter_count,
            "name_colour": name_colour,
            "name_fluent": name_fluent,
            "name_fluent_reasoning": name_fluent_reasoning,
            "sun_readable": sun_readable,
            "sun_word": sun_word if sun_readable else None,
            "sun_colour": sun_colour
        }
        
    except Exception as e:
        logging.error(f"Blind stranger test failed: {str(e)}")
        return {"is_established": False, "error": str(e)}

# =============================================================================
# SCORE ASSESSMENT - MAIN ENDPOINT
# =============================================================================

# =============================================================================
# TRIPLE AI VERIFICATION - HELPER FUNCTIONS
# =============================================================================

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

def extract_letters_from_text(text):
    """
    Extract letter mentions from observation text.
    Looks for patterns like 'O', "recognisable letter 'A'", etc.
    """
    if not text:
        return []
    
    # Find letters mentioned in quotes or parentheses
    patterns = [
        r"letter[s]?\s+['\"]([A-Za-z])['\"]",  # letter 'A'
        r"letter[s]?\s+\(([A-Za-z])\)",         # letter (A)
        r"['\"]([A-Z])['\"]",                    # 'O' or "O"
        r"\(([A-Z])\)",                          # (O)
    ]
    
    letters = []
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        letters.extend([m.upper() for m in matches])
    
    # Remove duplicates while preserving order
    seen = set()
    unique_letters = []
    for letter in letters:
        if letter not in seen:
            seen.add(letter)
            unique_letters.append(letter)
    
    return unique_letters

def compare_three_assessments(assessments):
    """
    Compare 3 AI assessments and build consensus.
    Checks scores, letters and observations for agreement.
    Returns merged assessment with averaged scores and consensus observations.
    """
    # Extract letters identified in name_writing observations
    letters_by_ai = []
    for assessment in assessments:
        obs = assessment.get('observations', {}).get('name_writing', '')
        letters = extract_letters_from_text(obs)
        letters_by_ai.append(letters)
    
    # Count agreement for each letter
    all_letters = set()
    for letters in letters_by_ai:
        all_letters.update(letters)
    
    letter_consensus = {}
    for letter in all_letters:
        count = sum(1 for letters in letters_by_ai if letter in letters)
        letter_consensus[letter] = count
    
    # Filter: only keep letters seen by 2+ AIs
    agreed_letters = [letter for letter, count in letter_consensus.items() if count >= 2]
    
    # Extract scores from all assessments
    pair1_totals = []
    pair2_totals = []
    cross_totals = []
    total_scores = []
    
    for assessment in assessments:
        pair1_totals.append(assessment.get('pair1_subtotal', 0))
        pair2_totals.append(assessment.get('pair2_subtotal', 0))
        cross_totals.append(assessment.get('cross_pair_subtotal', 0))
        total_scores.append(assessment.get('total_score', 0))
    
    # Check for large score discrepancies
    total_range = max(total_scores) - min(total_scores)
    if total_range > 3:
        logging.warning(f"Large score discrepancy detected: scores range from {min(total_scores)} to {max(total_scores)}")
    
    # Use median score
    def median(values):
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n % 2 == 0:
            return (sorted_vals[n//2-1] + sorted_vals[n//2]) / 2
        return sorted_vals[n//2]
    
    consensus_pair1 = round(median(pair1_totals))
    consensus_pair2 = round(median(pair2_totals))
    consensus_cross = round(median(cross_totals))
    consensus_total = round(median(total_scores))
    
    # Find assessment with total score closest to consensus
    best_idx = 0
    smallest_diff = abs(total_scores[0] - consensus_total)
    
    for idx, score in enumerate(total_scores):
        diff = abs(score - consensus_total)
        if diff < smallest_diff:
            smallest_diff = diff
            best_idx = idx
    
    # Use this assessment as base
    consensus_assessment = assessments[best_idx].copy()
    
    # Update totals with consensus scores
    consensus_assessment['pair1_subtotal'] = consensus_pair1
    consensus_assessment['pair2_subtotal'] = consensus_pair2
    consensus_assessment['cross_pair_subtotal'] = consensus_cross
    consensus_assessment['total_score'] = consensus_total
    
    # SMART CONSENSUS NOTE - Only add when appropriate
    writing_stage = consensus_assessment.get('writing_stage', '').upper()
    is_conventional = writing_stage == 'CONVENTIONAL'
    is_emerging = writing_stage == 'EMERGING'
    
    # Only add consensus note for non-conventional writers
    if not is_conventional and not is_emerging:
        obs = consensus_assessment.get('observations', {}).get('name_writing', '')
        if agreed_letters:
            consensus_note = f"[Verified letters: {', '.join(agreed_letters)}]"
        else:
            consensus_note = "[Multiple assessments found no clearly recognisable letters]"
        
        if obs:
            consensus_assessment['observations']['name_writing'] = f"{obs} {consensus_note}"
    
    logging.info(f"Consensus scores - Pair1: {consensus_pair1}, Pair2: {consensus_pair2}, Cross: {consensus_cross}, Total: {consensus_total}")
    logging.info(f"Score range was {min(total_scores)}-{max(total_scores)}, selected assessment {best_idx+1}")
    
    return consensus_assessment


       

def call_ai_once(client, deployment_name, personalized_rubric, user_content):
    """
    Make a single AI API call. Used for parallel execution.
    """
    try:
        response = client.chat.completions.create(
            model=deployment_name,
            messages=[
                {"role": "system", "content": personalized_rubric},
                {"role": "user", "content": user_content}
            ],
            max_tokens=2000,
            temperature=0.2
        )
        
        ai_response = response.choices[0].message.content
        logging.info(f"Got response length: {len(ai_response)}")
        
        return parse_json_response(ai_response)
        
    except Exception as e:
        logging.error(f"AI call failed: {str(e)}")
        return None


@app.route(route="score_assessment", methods=["GET", "POST"])
def score_assessment(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Score assessment endpoint called.')
    
    
    # Admin mode: /api/score_assessment?mode=admin&action=stats&admin_password=xxx
    # Works for both GET and POST requests
    if req.params.get("mode") == "admin":
        return _handle_admin(req)
    
    if req.method == "GET":
        return func.HttpResponse(
            json.dumps({
                "endpoint": "score_assessment",
                "method": "POST",
                "description": "Score a child's writing/drawing assessment",
                "partial_assessments": "supported - minimum one complete pair"
            }),
            mimetype="application/json",
            status_code=200
        )
    try:
        if not OPENAI_AVAILABLE:
            return func.HttpResponse(json.dumps({"error": "OpenAI not available"}), mimetype="application/json", status_code=503)
        
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        deployment_name = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-2")
        
        if not api_key or not endpoint:
            return func.HttpResponse(json.dumps({"error": "Missing Azure OpenAI configuration"}), mimetype="application/json", status_code=503)
        
        client = AzureOpenAI(api_key=api_key, api_version="2024-10-21", azure_endpoint=endpoint)
        
        try:
            req_body = req.get_json()
        except ValueError:
            return func.HttpResponse(json.dumps({"error": "Invalid JSON"}), mimetype="application/json", status_code=400)
        
        if not req_body:
            return func.HttpResponse(json.dumps({"error": "Request body required"}), mimetype="application/json", status_code=400)
        
        images = req_body.get('images', {})
        
        pair1_complete = bool(images.get('name_writing') and images.get('self_portrait'))
        pair2_complete = bool(images.get('sun_writing') and images.get('sun_drawing'))
        
        if not pair1_complete and not pair2_complete:
            return func.HttpResponse(
                json.dumps({"error": "At least one complete pair required", "received": list(images.keys())}),
                mimetype="application/json",
                status_code=400
            )
        
        child_name = req_body.get('child_name', 'the child')
        child_age_months = req_body.get('child_age_months', 36)
        assessment_id = req_body.get('assessment_id', str(uuid.uuid4()))
        generate_pdf = req_body.get('generate_pdf', False)
        questionnaire = req_body.get('questionnaire', {})
        recipient_email = req_body.get('email', '')
        favourite_colour_stated = questionnaire.get('favourite_colour', '')
        
        # Detect test sessions
        is_test_session = False
        session_token = req_body.get('session_token', '')
        if session_token:
            try:
                sessions_container = get_sessions_container()
                if sessions_container:
                    session_doc = sessions_container.read_item(item=session_token, partition_key=session_token)
                    is_test_session = session_doc.get('is_test', False)
            except Exception:
                pass
        if not is_test_session and session_token.startswith('test_free_'):
            is_test_session = True
        
        if child_age_months < 24 or child_age_months > 48:
            return func.HttpResponse(json.dumps({"error": "Age must be 24-48 months"}), mimetype="application/json", status_code=400)
        
        age_years = child_age_months // 12
        age_months_remainder = child_age_months % 12
        
        logging.info(f'Scoring for {child_name}, age {age_years}y {age_months_remainder}m, pair1={pair1_complete}, pair2={pair2_complete}, email={recipient_email}')
        
        # Store images for debugging (auto-deleted after 7 days via Azure lifecycle policy)
        try:
            for image_name, image_base64 in images.items():
                if image_base64:
                    image_data = base64.b64decode(image_base64)
                    upload_image_to_blob(assessment_id, image_name, image_data)
            logging.info(f'Images stored for assessment {assessment_id}')
        except Exception as e:
            logging.warning(f'Failed to store images: {str(e)}')
      
        # Build favourite colour context for AI
        fav_colour_context = ""
        if favourite_colour_stated and favourite_colour_stated not in ['', 'no_preference']:
            fav_colour_context = f"\n\nThe parent stated that {child_name}'s favourite colour is: {favourite_colour_stated}"
        
        if pair1_complete and pair2_complete:
            prompt_text = f"""Please score this assessment for {child_name}, age {age_years} years and {age_months_remainder} months.

The 4 images are in order: NAME_WRITING, SELF_PORTRAIT, SUN_WRITING, SUN_DRAWING.{fav_colour_context}

Apply the Treiman rubric and return scores in the specified JSON format."""
        elif pair1_complete:
            prompt_text = f"""Please score this PARTIAL assessment for {child_name}, age {age_years} years and {age_months_remainder} months.

Only 2 images (Pair 1): NAME_WRITING, SELF_PORTRAIT.{fav_colour_context}

Score ONLY pair1_scores. Set pair2_scores to null, pair2_subtotal to 0, cross_pair_scores to null, cross_pair_subtotal to 0. Max score is 11.

Return JSON format with pair1_scores filled in and pair2_scores as null."""
        else:
            prompt_text = f"""Please score this PARTIAL assessment for {child_name}, age {age_years} years and {age_months_remainder} months.

Only 2 images (Pair 2): SUN_WRITING, SUN_DRAWING.{fav_colour_context}

Score ONLY pair2_scores. Set pair1_scores to null, pair1_subtotal to 0, cross_pair_scores to null, cross_pair_subtotal to 0. Max score is 11.

Return JSON format with pair2_scores filled in and pair1_scores as null."""
        
        user_content = [{"type": "text", "text": prompt_text}]
        
        if pair1_complete:
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['name_writing']}"}})
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['self_portrait']}"}})
        if pair2_complete:
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['sun_writing']}"}})
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images['sun_drawing']}"}})
        
        # Sanitise favourite colour input
        def sanitise_colour_input(colour_input):
            if not colour_input:
                return "not specified"
            sanitised = re.sub(r'[^a-zA-Z\s-]', '', str(colour_input))
            sanitised = sanitised[:30].strip()
            return sanitised if sanitised else "not specified"
        
        safe_colour = sanitise_colour_input(favourite_colour_stated)
        logging.info(f"Sanitised colour: '{favourite_colour_stated}' -> '{safe_colour}'")
        
        personalized_rubric = TREIMAN_RUBRIC.replace("{favourite_colour_stated}", safe_colour)

        # =============================================================================
        # TRIPLE PARALLEL AI VERIFICATION
        # =============================================================================

        # =============================================================================
        # BLIND STRANGER TEST - No confirmation bias possible
        # =============================================================================
        
        blind_result = blind_stranger_test(
            client,
            deployment_name,
            name_image=images.get('name_writing'),
            sun_image=images.get('sun_writing')
        )
        
        logging.info(f"Blind test: established={blind_result.get('is_established')}, name={blind_result.get('name_word')}, sun={blind_result.get('sun_word')}")
        
        # If established writer, skip expensive triple verification
        if blind_result.get('is_established'):
            logging.info("Blind test detected CONVENTIONAL writer - returning early")
            
            stage_info = get_conventional_writing_feedback(
                child_name, 
                f"Writing shows recognisable letters: name={blind_result.get('name_word')}, sun={blind_result.get('sun_word')}"
            )
            verbal_interpretation = interpret_verbal_behaviour(questionnaire, 0, "ALREADY WRITING", child_name)
            
            final_response = {
                "success": True,
                "assessment_id": assessment_id,
                "partial_assessment": not (pair1_complete and pair2_complete),
                "pairs_completed": {"pair1": pair1_complete, "pair2": pair2_complete},
                "child": {
                    "name": child_name,
                    "age_months": child_age_months,
                    "age_display": f"{age_years} years, {age_months_remainder} months"
                },
                "email": recipient_email,
                "blind_test_result": blind_result,
                "visual_analysis": {
                    "writing_stage": "CONVENTIONAL",
                    "writing_stage_reasoning": f"Writing shows recognisable letters the writing without knowing child's name",
                    "observations": {
                        "name_writing": f"Clearly readable: \"{blind_result.get('name_word')}\" in {blind_result.get('name_colour', 'pencil/crayon')}" if blind_result.get('name_readable') else None,
                        "self_portrait": None,
                        "sun_writing": f"Clearly readable: \"sun\" in {blind_result.get('sun_colour', 'pencil/crayon')}" if blind_result.get('sun_readable') else None,
                        "sun_drawing": None
                    },
                    "favourite_colour_detected": blind_result.get('name_colour') or blind_result.get('sun_colour') or "none"
                },
                "scoring": {
                    "total_score": None,
                    "max_score": None,
                    "percentage": None,
                    "note": "Scores not applicable - child is already writing real words"
                },
                "interpretation": {
                    "stage": "ALREADY WRITING",
                    "stage_description": stage_info["description"],
                    "stage_detail": stage_info["detail"],
                    "writing_stage": "CONVENTIONAL",
                    "writing_stage_reasoning": stage_info.get("writing_stage_reasoning", ""),
                    "is_conventional_writer": True,
                    "recommendation": stage_info.get("recommendation", "")
                },
                "verbal_behaviour": verbal_interpretation,
                "metadata": {
                    "model_used": deployment_name,
                    "rubric_version": "blind-test-v1",
                    "api_version": "7.7.0",
                    "assessment_method": "blind_stranger_test"
                }
            }
            
            # Generate PDF if requested
            pdf_bytes = None
            if generate_pdf and REPORTLAB_AVAILABLE and STORAGE_AVAILABLE:
                try:
                    pdf_bytes = generate_assessment_pdf(final_response)
                    pdf_url = upload_pdf_to_blob(assessment_id, pdf_bytes)
                    final_response["pdf_url"] = pdf_url
                except Exception as e:
                    logging.warning(f'PDF generation failed: {str(e)}')
            
            # Send email with PDF attachment
            if recipient_email and EMAIL_AVAILABLE:
                try:
                    if pdf_bytes is None and REPORTLAB_AVAILABLE:
                        pdf_bytes = generate_assessment_pdf(final_response)
                    email_result = send_assessment_email(recipient_email, child_name, assessment_id, pdf_bytes)
                    final_response["email_sent"] = True
                    final_response["email_status"] = email_result
                    logging.info(f'Email sent successfully to {recipient_email}')
                except Exception as e:
                    logging.error(f'Failed to send email: {str(e)}')
                    final_response["email_sent"] = False
                    final_response["email_error"] = str(e)
            
            # Save to database (after PDF and email so all fields are populated)
            final_response["is_test"] = is_test_session
            final_response["image_names"] = [k for k, v in images.items() if v]
            if COSMOS_AVAILABLE:
                try:
                    save_assessment_to_db(final_response)
                    final_response["saved_to_database"] = True
                    
                    # Save anonymised admin stats (permanent) and refund lookup (14 days)
                    save_admin_stat(final_response)
                    if recipient_email:
                        save_refund_lookup(assessment_id, recipient_email)
                        
                except Exception as e:
                    logging.error(f'DB save failed: {str(e)}')
                    final_response["saved_to_database"] = False
            
            return func.HttpResponse(json.dumps(final_response), mimetype="application/json", status_code=200)
            
        
        # If not established, continue with full triple verification below



        
        logging.info("Starting triple parallel AI verification...")
        
        assessments = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(call_ai_once, client, deployment_name, personalized_rubric, user_content)
                for _ in range(3)
            ]
            
            for future in as_completed(futures):
                result = future.result()
                if result:
                    assessments.append(result)
                    logging.info(f"Collected assessment {len(assessments)}/3")
        
        if len(assessments) < 2:
            logging.error(f"Triple verification failed: only {len(assessments)} successful responses out of 3")
            return func.HttpResponse(
                json.dumps({
                    "error": "AI assessment failed - insufficient responses",
                    "details": f"Only {len(assessments)} of 3 AI assessments completed successfully",
                    "suggestion": "Please try again. If the problem persists, contact support."
                }),
                mimetype="application/json",
                status_code=500
            )
        
        if len(assessments) == 3:
            logging.info("All 3 assessments successful - building consensus")
            visual_scores = compare_three_assessments(assessments)
        else:
            logging.info("Only 2 assessments successful - using best one")
            visual_scores = max(assessments, key=lambda a: len(str(a.get('observations', {}))))
        
        logging.info("Final consensus assessment selected")
       
        
        # Clean all internal bracketed notes from observations
        if visual_scores.get('observations'):
            for key in visual_scores['observations']:
                if visual_scores['observations'][key]:
                    # Remove [Verified...], [Multiple assessments...], and any other bracketed notes
                    cleaned = re.sub(r'\s*\[[^\]]*\]', '', str(visual_scores['observations'][key])).strip()
                    visual_scores['observations'][key] = cleaned
   
        pair1_subtotal = visual_scores.get('pair1_subtotal', 0) if pair1_complete else 0
   
        pair1_subtotal = visual_scores.get('pair1_subtotal', 0) if pair1_complete else 0
        pair2_subtotal = visual_scores.get('pair2_subtotal', 0) if pair2_complete else 0
        cross_pair_subtotal = visual_scores.get('cross_pair_subtotal', 0) if (pair1_complete and pair2_complete) else 0

        pair2_both_are_drawings = visual_scores.get('pair2_both_are_drawings', False)
        
        writing_stage = visual_scores.get('writing_stage', 'UNKNOWN').upper()
        writing_stage_reasoning = visual_scores.get('writing_stage_reasoning', '')

        pair1_max = 10 if pair1_complete else 0
        pair2_max = 0 if pair2_both_are_drawings else (11 if pair2_complete else 0)
        cross_max = 3 if (pair1_complete and pair2_complete and not pair2_both_are_drawings) else 0
        max_score = pair1_max + pair2_max + cross_max
        
        total_score = pair1_subtotal + (0 if pair2_both_are_drawings else pair2_subtotal) + cross_pair_subtotal                  
        
        
        is_conventional_writer = writing_stage == 'CONVENTIONAL'
        
        if is_conventional_writer:
            # Check if short name cap applies
            name_letter_count = blind_result.get('name_letter_count', 0)
            sun_readable = blind_result.get('sun_readable', False)
            
            if name_letter_count <= 3 and not sun_readable:
                # Short name without sun - cap at EMERGING → Strong Start
                logging.info(f"Capping CONVENTIONAL to STRONG START: short name ({name_letter_count} letters) without sun")
                stage_info = {
                    "stage": "STRONG START",
                    "description": f"{child_name} is forming recognisable letters",
                    "detail": f"{child_name} can write their name with clear, recognisable letters - this is wonderful progress!",
                    "short_name_capped": True
                }
            else:
                # Normal CONVENTIONAL → Already Writing
                stage_info = get_conventional_writing_feedback(child_name, writing_stage_reasoning)
                stage_info["short_name_capped"] = False
                logging.info(f'CONVENTIONAL writing detected for {child_name}')
        else:
            # Use the new floor logic function for all non-conventional writers
            stage_info = determine_stage_with_writing_stage(
                total_score, 
                max_score, 
                child_age_months, 
                writing_stage, 
                child_name,
                blind_result
            )
            
            logging.info(f'Stage determined: {stage_info["stage"]} (writing_stage={writing_stage})')
        
        verbal_interpretation = interpret_verbal_behaviour(questionnaire, total_score, stage_info["stage"], child_name)
        percentage = round((total_score / max_score) * 100, 1) if max_score > 0 else 0
        
        interpretation_data = {
            "stage": stage_info["stage"],
            "stage_description": stage_info["description"],
            "stage_detail": stage_info["detail"],
            "writing_stage": writing_stage,
            "writing_stage_reasoning": writing_stage_reasoning,
            "short_name_capped": stage_info.get("short_name_capped", False)
        }
        
        if stage_info["stage"] == "ALREADY WRITING":
            interpretation_data["is_conventional_writer"] = True
            interpretation_data["recommendation"] = stage_info.get("recommendation", "")
        
        # Add writing development notes
        if writing_stage == "EMERGING":
            interpretation_data["writing_development"] = f"{child_name} is forming real letters - this is wonderful progress!"
        elif writing_stage == "LETTER_LIKE":
            interpretation_data["writing_development"] = f"{child_name} is making letter-like shapes in their writing attempts."
    
        final_response = {
            "success": True,
            "assessment_id": assessment_id,
            "partial_assessment": not (pair1_complete and pair2_complete),
            "pairs_completed": {"pair1": pair1_complete, "pair2": pair2_complete},
            "child": {
                "name": child_name,
                "age_months": child_age_months,
                "age_display": f"{age_years} years, {age_months_remainder} months"
            },
            "email": recipient_email,
            "visual_analysis": visual_scores,
            "scoring": {
                "total_score": total_score,
                "max_score": max_score,
                "percentage": percentage,
                "pair1_subtotal": pair1_subtotal if pair1_complete else None,
                "pair1_max": pair1_max if pair1_complete else None,
                "pair2_subtotal": pair2_subtotal if pair2_complete else None,
                "pair2_max": pair2_max if pair2_complete else None,
                "pair2_both_are_drawings": pair2_both_are_drawings if pair2_complete else False,
                "cross_pair_subtotal": cross_pair_subtotal if (pair1_complete and pair2_complete and not pair2_both_are_drawings) else None,
                "cross_pair_max": cross_max if (pair1_complete and pair2_complete and not pair2_both_are_drawings) else None
            },
            "interpretation": interpretation_data,
            "verbal_behaviour": verbal_interpretation,
            "metadata": {"model_used": deployment_name, "rubric_version": "Treiman-2011-24pt-v1", "api_version": "7.7.0"}
        }
        
        if pair1_complete and pair2_complete:
            final_response["pair_comparison"] = {
                "pair1_percentage": round((pair1_subtotal / 10) * 100, 1),
                "pair2_percentage": round((pair2_subtotal / 11) * 100, 1),
                "interpretation": get_pair_comparison_interpretation(visual_scores)
            }
        
        
        # Generate PDF if requested
        pdf_bytes = None
        if generate_pdf and REPORTLAB_AVAILABLE and STORAGE_AVAILABLE:
            try:
                pdf_bytes = generate_assessment_pdf(final_response)
                pdf_url = upload_pdf_to_blob(assessment_id, pdf_bytes)
                final_response["pdf_url"] = pdf_url
            except Exception as e:
                logging.warning(f'PDF generation failed: {str(e)}')
        
        # Send email with PDF attachment
        if recipient_email and EMAIL_AVAILABLE:
            try:
                if pdf_bytes is None and REPORTLAB_AVAILABLE:
                    pdf_bytes = generate_assessment_pdf(final_response)
                email_result = send_assessment_email(recipient_email, child_name, assessment_id, pdf_bytes)
                final_response["email_sent"] = True
                final_response["email_status"] = email_result
                logging.info(f'Email sent successfully to {recipient_email}')
            except Exception as e:
                logging.error(f'Failed to send email: {str(e)}')
                final_response["email_sent"] = False
                final_response["email_error"] = str(e)
        
        # Save to database (after PDF and email so all fields are populated)
        final_response["is_test"] = is_test_session
        final_response["image_names"] = [k for k, v in images.items() if v]
        if COSMOS_AVAILABLE:
            try:
                save_assessment_to_db(final_response)
                final_response["saved_to_database"] = True
                
                # Save anonymised admin stats (permanent) and refund lookup (14 days)
                save_admin_stat(final_response)
                if recipient_email:
                    save_refund_lookup(assessment_id, recipient_email)
                    
            except Exception as e:
                logging.error(f'DB save failed: {str(e)}')
                final_response["saved_to_database"] = False
        
        return func.HttpResponse(json.dumps(final_response), mimetype="application/json", status_code=200)
                
    except Exception as e:
        logging.error(f'Error scoring assessment: {str(e)}')
        return func.HttpResponse(json.dumps({"error": "Failed to score assessment", "details": str(e)}), mimetype="application/json", status_code=500)


# =============================================================================
# ADMIN DASHBOARD - Accessed via /api/score_assessment?mode=admin&action=xxx
# =============================================================================

def _handle_admin(req):
    """Route admin actions. Called from score_assessment when mode=admin (GET or POST)."""
    if not verify_admin_password(req):
        return func.HttpResponse(
            json.dumps({"error": "Unauthorised"}),
            mimetype="application/json",
            status_code=401
        )
    
    action = req.params.get("action", "")
    
    try:
        if action == "stats":
            return _admin_stats(req)
        elif action == "refund_lookup":
            return _admin_refund_lookup(req)
        elif action == "mark_refunded":
            return _admin_mark_refunded(req)
        elif action == "purchase_stats":
            return _admin_purchase_stats(req)
        elif action == "create_test_session":
            return _admin_create_test_session(req)
        elif action == "customer_search":
            return _admin_customer_search(req)
        elif action == "get_image":
            return _admin_get_image(req)
        elif action == "get_report":
            return _admin_get_report(req)
        else:
            return func.HttpResponse(
                json.dumps({"error": f"Unknown action: {action}"}),
                mimetype="application/json",
                status_code=400
            )
    except Exception as e:
        logging.error(f"Admin error ({action}): {str(e)}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500
        )


def _admin_stats(req):
    container = get_admin_stats_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "AdminStats container not available"}), mimetype="application/json", status_code=503)
    
    product = req.params.get("product", "starter")
    days = int(req.params.get("days", "90"))
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = "SELECT * FROM c WHERE c.product = @product AND c.created_at >= @cutoff ORDER BY c.created_at DESC"
    params = [{"name": "@product", "value": product}, {"name": "@cutoff", "value": cutoff_date}]
    items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
    
    total = len(items)
    completed = len([i for i in items if i.get("status") == "completed"])
    refunded = len([i for i in items if i.get("refunded")])
    tests = len([i for i in items if i.get("is_test")])
    real_customers = total - tests
    
    age_bands = {}
    for item in items:
        band = item.get("age_band", "unknown")
        age_bands[band] = age_bands.get(band, 0) + 1
    
    stages = {}
    for item in items:
        stage = item.get("stage", "UNKNOWN")
        stages[stage] = stages.get(stage, 0) + 1
    
    by_date = {}
    for item in items:
        date_str = item.get("created_at", "")[:10]
        if date_str:
            by_date[date_str] = by_date.get(date_str, 0) + 1
    
    partial = len([i for i in items if i.get("partial_assessment")])
    full = total - partial
    
    scores = [i.get("score_percentage", 0) for i in items if i.get("score_percentage")]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    
    return func.HttpResponse(json.dumps({
        "period_days": days,
        "product": product,
        "summary": {
            "total_assessments": total,
            "real_customers": real_customers,
            "test_assessments": tests,
            "completed": completed,
            "refunded": refunded,
            "revenue_estimate": (real_customers - refunded) * 20
        },
        "age_bands": age_bands,
        "stages": stages,
        "by_date": dict(sorted(by_date.items())),
        "completion": {"full_assessments": full, "partial_assessments": partial},
        "average_score_percentage": avg_score,
        "recent": [
            {
                "assessment_id": i["assessment_id"],
                "created_at": i.get("created_at"),
                "age_band": i.get("age_band"),
                "stage": i.get("stage"),
                "score_percentage": i.get("score_percentage"),
                "status": i.get("status"),
                "is_test": i.get("is_test", False),
                "refunded": i.get("refunded", False)
            }
            for i in items[:50]
        ]
    }), mimetype="application/json", status_code=200)


def _admin_refund_lookup(req):
    container = get_refund_lookup_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "RefundLookup container not available"}), mimetype="application/json", status_code=503)
    
    days = int(req.params.get("days", "7"))
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = "SELECT * FROM c WHERE c.created_at >= @cutoff ORDER BY c.created_at DESC"
    params = [{"name": "@cutoff", "value": cutoff_date}]
    items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
    
    results = []
    for item in items:
        email = item.get("email", "")
        masked = email[0] + "****@" + email.split("@")[-1] if "@" in email else "****"
        results.append({
            "assessment_id": item["assessment_id"],
            "email_masked": masked,
            "email_full": email,
            "created_at": item.get("created_at"),
            "refunded": item.get("refunded", False)
        })
    
    return func.HttpResponse(json.dumps({"customers": results, "count": len(results), "period_days": days}), mimetype="application/json", status_code=200)


def _admin_mark_refunded(req):
    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse(json.dumps({"error": "Invalid JSON"}), mimetype="application/json", status_code=400)
    
    assessment_id = req_body.get("assessment_id")
    if not assessment_id:
        return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
    
    updated = []
    try:
        stats_container = get_admin_stats_container()
        if stats_container:
            item = stats_container.read_item(item=assessment_id, partition_key=assessment_id)
            item["refunded"] = True
            item["refunded_at"] = datetime.utcnow().isoformat()
            stats_container.upsert_item(body=item)
            updated.append("AdminStats")
    except Exception as e:
        logging.warning(f"Could not update AdminStats: {str(e)}")
    
    try:
        refund_container = get_refund_lookup_container()
        if refund_container:
            item = refund_container.read_item(item=assessment_id, partition_key=assessment_id)
            item["refunded"] = True
            item["refunded_at"] = datetime.utcnow().isoformat()
            refund_container.upsert_item(body=item)
            updated.append("RefundLookup")
    except Exception as e:
        logging.warning(f"Could not update RefundLookup: {str(e)}")
    
    return func.HttpResponse(json.dumps({"success": True, "assessment_id": assessment_id, "updated": updated}), mimetype="application/json", status_code=200)


def _admin_purchase_stats(req):
    container = get_sessions_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "Sessions container not available"}), mimetype="application/json", status_code=503)
    
    days = int(req.params.get("days", "30"))
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
    
    query = "SELECT * FROM c WHERE c.created_at >= @cutoff ORDER BY c.created_at DESC"
    params = [{"name": "@cutoff", "value": cutoff_date}]
    items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
    
    total_purchased = len(items)
    used = len([i for i in items if i.get("used")])
    unused = total_purchased - used
    
    by_date = {}
    for item in items:
        date_str = item.get("created_at", "")[:10]
        if date_str:
            by_date[date_str] = by_date.get(date_str, 0) + 1
    
    return func.HttpResponse(json.dumps({
        "period_days": days,
        "total_purchased": total_purchased,
        "used_sessions": used,
        "unused_sessions": unused,
        "by_date": dict(sorted(by_date.items()))
    }), mimetype="application/json", status_code=200)


def _admin_create_test_session(req):
    container = get_sessions_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "Sessions container not available"}), mimetype="application/json", status_code=503)
    
    test_token = f"test_{uuid.uuid4().hex[:12]}"
    expiry_date = datetime.utcnow() + timedelta(days=1)
    
    session_data = {
        "id": test_token,
        "session_token": test_token,
        "created_at": datetime.utcnow().isoformat(),
        "expires_at": expiry_date.isoformat(),
        "used": False,
        "is_test": True,
        "ttl": 1 * 24 * 60 * 60
    }
    
    container.upsert_item(body=session_data)
    access_url = f"https://earlywriting.morehandwriting.co.uk/?session={test_token}"
    
    return func.HttpResponse(json.dumps({
        "success": True,
        "session_token": test_token,
        "access_url": access_url,
        "expires_in": "24 hours"
    }), mimetype="application/json", status_code=200)


def _admin_customer_search(req):
    """Search for customer assessments by email within the 7-day window."""
    email_query = req.params.get("email", "").strip().lower()
    if not email_query:
        return func.HttpResponse(json.dumps({"error": "email parameter required"}), mimetype="application/json", status_code=400)
    
    container = get_cosmos_container()
    if not container:
        return func.HttpResponse(json.dumps({"error": "Assessments container not available"}), mimetype="application/json", status_code=503)
    
    try:
        # Partial match: supports searching with part of email
        query = "SELECT * FROM c WHERE CONTAINS(LOWER(c.email), @email) ORDER BY c.created_at DESC"
        params = [{"name": "@email", "value": email_query}]
        items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
        
        # Check blob storage for images and reports for each result
        blob_service = get_blob_service_client()
        
        results = []
        for item in items:
            assessment_id = item.get("assessment_id", item.get("id", ""))
            scoring = item.get("scoring", {})
            interpretation = item.get("interpretation", {})
            
            # Check blob storage for actual images
            found_images = []
            if blob_service:
                try:
                    uploads_container = blob_service.get_container_client("uploads")
                    prefix = f"{assessment_id}/"
                    blobs = uploads_container.list_blobs(name_starts_with=prefix)
                    for blob in blobs:
                        img_name = blob.name.replace(prefix, "").replace(".png", "")
                        found_images.append(img_name)
                except Exception:
                    pass
            
            # Check if report exists in blob
            has_report = False
            if blob_service:
                try:
                    reports_container = blob_service.get_container_client("reports")
                    blob_client = reports_container.get_blob_client(f"{assessment_id}/report.pdf")
                    blob_client.get_blob_properties()
                    has_report = True
                except Exception:
                    pass
            
            results.append({
                "assessment_id": assessment_id,
                "email": item.get("email", ""),
                "created_at": item.get("created_at", ""),
                "child_age_months": item.get("child", {}).get("age_months", 0),
                "stage": interpretation.get("stage", "UNKNOWN"),
                "writing_stage": interpretation.get("writing_stage", ""),
                "score_percentage": scoring.get("percentage", 0),
                "total_score": scoring.get("total_score", 0),
                "max_score": scoring.get("max_score", 0),
                "has_report": has_report,
                "is_test": item.get("is_test", False),
                "image_names": found_images
            })
        
        return func.HttpResponse(json.dumps({
            "results": results,
            "count": len(results),
            "email_searched": email_query
        }), mimetype="application/json", status_code=200)
    
    except Exception as e:
        logging.error(f"Customer search error: {str(e)}")
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)


def _admin_get_image(req):
    """Serve an uploaded image from blob storage."""
    assessment_id = req.params.get("assessment_id", "")
    image_name = req.params.get("image_name", "")
    if not assessment_id or not image_name:
        return func.HttpResponse(json.dumps({"error": "assessment_id and image_name required"}), mimetype="application/json", status_code=400)
    
    try:
        blob_service = get_blob_service_client()
        if not blob_service:
            return func.HttpResponse(json.dumps({"error": "Storage not available"}), mimetype="application/json", status_code=503)
        container_client = blob_service.get_container_client("uploads")
        blob_name = f"{assessment_id}/{image_name}.png"
        blob_client = container_client.get_blob_client(blob_name)
        image_data = blob_client.download_blob().readall()
        return func.HttpResponse(image_data, mimetype="image/png", status_code=200)
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": f"Image not found: {str(e)}"}), mimetype="application/json", status_code=404)


def _admin_get_report(req):
    """Serve a PDF report from blob storage."""
    assessment_id = req.params.get("assessment_id", "")
    if not assessment_id:
        return func.HttpResponse(json.dumps({"error": "assessment_id required"}), mimetype="application/json", status_code=400)
    
    try:
        pdf_data = get_pdf_from_blob(assessment_id)
        return func.HttpResponse(
            pdf_data,
            mimetype="application/pdf",
            status_code=200,
            headers={"Content-Disposition": f"inline; filename=report_{assessment_id[:8]}.pdf"}
        )
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": f"Report not found: {str(e)}"}), mimetype="application/json", status_code=404)
