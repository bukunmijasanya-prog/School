"""
API Routes for Nursery Extension
"""

from flask import Blueprint, request, jsonify, render_template
from werkzeug.utils import secure_filename
from datetime import datetime
import json
import base64
import os

from .models import db, School, Teacher, SchoolClass, Child, Assessment, BatchJob
from .batch import analyse_batch, split_into_batches, merge_batch_results

nursery_bp = Blueprint('nursery', __name__, url_prefix='/nursery')

# Get API key from environment
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')


# ============================================================================
# DASHBOARD
# ============================================================================

@nursery_bp.route('/dashboard')
def dashboard():
    """Teacher dashboard showing all classes"""
    # In production, get school_id from logged-in teacher
    school_id = request.args.get('school_id')
    
    classes = SchoolClass.query.filter_by(school_id=school_id).all()
    
    return render_template('nursery/dashboard.html', classes=classes)


# ============================================================================
# CLASS MANAGEMENT
# ============================================================================

@nursery_bp.route('/classes', methods=['GET'])
def get_classes():
    """Get all classes for a school"""
    school_id = request.args.get('school_id')
    
    classes = SchoolClass.query.filter_by(school_id=school_id).all()
    
    return jsonify([{
        'id': c.id,
        'name': c.name,
        'academic_year': c.academic_year,
        'child_count': len(c.children)
    } for c in classes])


@nursery_bp.route('/classes', methods=['POST'])
def create_class():
    """Create a new class"""
    data = request.json
    
    new_class = SchoolClass(
        school_id=data['school_id'],
        name=data['name'],
        academic_year=data.get('academic_year')
    )
    
    db.session.add(new_class)
    db.session.commit()
    
    return jsonify({'id': new_class.id, 'name': new_class.name}), 201


# ============================================================================
# CHILDREN MANAGEMENT
# ============================================================================

@nursery_bp.route('/classes/<class_id>/children', methods=['GET'])
def get_children(class_id):
    """Get all children in a class with their assessment status"""
    
    children = Child.query.filter_by(class_id=class_id).all()
    
    result = []
    for child in children:
        child_data = {
            'id': child.id,
            'name': child.first_name,
            'age': child.age_string,
            'date_of_birth': child.date_of_birth.isoformat(),
            'has_image': False,
            'has_observations': False,
            'status': 'pending',
            'score': None,
            'stage': None
        }
        
        if child.assessment:
            a = child.assessment
            child_data['has_image'] = bool(a.image_url)
            child_data['has_observations'] = bool(a.observations_verbal or a.observations_paper or a.observations_difficulty)
            child_data['status'] = a.status
            child_data['score'] = a.score
            child_data['stage'] = a.stage
        
        result.append(child_data)
    
    return jsonify(result)


@nursery_bp.route('/classes/<class_id>/children', methods=['POST'])
def add_child(class_id):
    """Add a child to a class"""
    data = request.json
    
    child = Child(
        class_id=class_id,
        first_name=data['first_name'],
        date_of_birth=datetime.strptime(data['date_of_birth'], '%Y-%m-%d').date()
    )
    
    db.session.add(child)
    db.session.commit()
    
    # Create empty assessment record
    assessment = Assessment(child_id=child.id)
    db.session.add(assessment)
    db.session.commit()
    
    return jsonify({
        'id': child.id,
        'name': child.first_name,
        'age': child.age_string
    }), 201


@nursery_bp.route('/classes/<class_id>/children/import', methods=['POST'])
def import_children(class_id):
    """Import multiple children from a list"""
    data = request.json
    children_data = data.get('children', [])
    
    added = []
    for child_info in children_data:
        child = Child(
            class_id=class_id,
            first_name=child_info['first_name'],
            date_of_birth=datetime.strptime(child_info['date_of_birth'], '%Y-%m-%d').date()
        )
        db.session.add(child)
        db.session.flush()  # Get the ID
        
        assessment = Assessment(child_id=child.id)
        db.session.add(assessment)
        
        added.append({'id': child.id, 'name': child.first_name})
    
    db.session.commit()
    
    return jsonify({'added': added, 'count': len(added)}), 201


# ============================================================================
# OBSERVATIONS
# ============================================================================

@nursery_bp.route('/children/<child_id>/observations', methods=['POST'])
def save_observations(child_id):
    """Save teacher observations for a child"""
    data = request.json
    
    assessment = Assessment.query.filter_by(child_id=child_id).first()
    
    if not assessment:
        assessment = Assessment(child_id=child_id)
        db.session.add(assessment)
    
    assessment.observations_verbal = json.dumps(data.get('verbal', []))
    assessment.observations_paper = json.dumps(data.get('paper', []))
    assessment.observations_difficulty = json.dumps(data.get('difficulty', []))
    
    db.session.commit()
    
    return jsonify({'success': True})


@nursery_bp.route('/children/<child_id>/observations', methods=['GET'])
def get_observations(child_id):
    """Get observations for a child"""
    
    assessment = Assessment.query.filter_by(child_id=child_id).first()
    
    if not assessment:
        return jsonify({'verbal': [], 'paper': [], 'difficulty': []})
    
    return jsonify({
        'verbal': json.loads(assessment.observations_verbal or '[]'),
        'paper': json.loads(assessment.observations_paper or '[]'),
        'difficulty': json.loads(assessment.observations_difficulty or '[]')
    })


# ============================================================================
# IMAGE UPLOAD
# ============================================================================

@nursery_bp.route('/children/<child_id>/upload', methods=['POST'])
def upload_image(child_id):
    """Upload assessment sheet image for a child"""
    
    if 'image' not in request.files:
        return jsonify({'error': 'No image provided'}), 400
    
    file = request.files['image']
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Read and encode the image
    image_data = file.read()
    image_base64 = base64.b64encode(image_data).decode('utf-8')
    
    # Get or create assessment
    assessment = Assessment.query.filter_by(child_id=child_id).first()
    
    if not assessment:
        assessment = Assessment(child_id=child_id)
        db.session.add(assessment)
    
    # For now, store base64 directly (in production, upload to Azure Blob Storage)
    # This is simplified - you'd want to store in Azure Blob Storage for production
    assessment.image_url = f"data:image/jpeg;base64,{image_base64}"
    assessment.uploaded_at = datetime.utcnow()
    
    db.session.commit()
    
    return jsonify({'success': True, 'child_id': child_id})


# ============================================================================
# BATCH ANALYSIS
# ============================================================================

@nursery_bp.route('/classes/<class_id>/analyse', methods=['POST'])
def analyse_class(class_id):
    """Analyse all ready children in a class"""
    
    # Get all children with images
    children = Child.query.filter_by(class_id=class_id).all()
    
    ready_children = []
    for child in children:
        if child.assessment and child.assessment.image_url:
            # Extract base64 from data URL
            image_data = child.assessment.image_url
            if image_data.startswith('data:'):
                image_base64 = image_data.split(',')[1]
            else:
                image_base64 = image_data
            
            ready_children.append({
                'id': child.id,
                'name': child.first_name,
                'age': child.age_string,
                'image_base64': image_base64,
                'observations_verbal': json.loads(child.assessment.observations_verbal or '[]'),
                'observations_paper': json.loads(child.assessment.observations_paper or '[]'),
                'observations_difficulty': json.loads(child.assessment.observations_difficulty or '[]')
            })
    
    if not ready_children:
        return jsonify({'error': 'No children ready for analysis'}), 400
    
    # Create batch job
    job = BatchJob(
        class_id=class_id,
        status='processing',
        total_children=len(ready_children),
        started_at=datetime.utcnow()
    )
    db.session.add(job)
    db.session.commit()
    
    try:
        # Split into batches if needed (max 10 per API call)
        batches = split_into_batches(ready_children, max_per_batch=10)
        
        all_results = []
        for batch in batches:
            result = analyse_batch(batch, OPENAI_API_KEY)
            all_results.append(result)
        
        # Merge results
        final_result = merge_batch_results(all_results)
        
        # Save results to each child's assessment
        for child_result in final_result.get('children', []):
            # Find matching child by name
            for child in children:
                if child.first_name.lower() == child_result.get('name', '').lower():
                    child.assessment.score = child_result.get('score')
                    child.assessment.stage = child_result.get('stage')
                    child.assessment.analysis_result = json.dumps(child_result)
                    child.assessment.status = 'analysed'
                    child.assessment.analysed_at = datetime.utcnow()
                    break
        
        # Update job
        job.status = 'completed'
        job.completed_at = datetime.utcnow()
        job.processed_children = len(ready_children)
        job.class_summary = json.dumps(final_result.get('class_summary'))
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'job_id': job.id,
            'children_processed': len(ready_children),
            'results': final_result
        })
    
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        db.session.commit()
        
        return jsonify({'error': str(e)}), 500


# ============================================================================
# RESULTS
# ============================================================================

@nursery_bp.route('/classes/<class_id>/results', methods=['GET'])
def get_class_results(class_id):
    """Get analysis results for a class"""
    
    children = Child.query.filter_by(class_id=class_id).all()
    
    results = []
    for child in children:
        if child.assessment and child.assessment.status == 'analysed':
            results.append({
                'id': child.id,
                'name': child.first_name,
                'age': child.age_string,
                'score': child.assessment.score,
                'stage': child.assessment.stage,
                'analysis': json.loads(child.assessment.analysis_result or '{}'),
                'analysed_at': child.assessment.analysed_at.isoformat() if child.assessment.analysed_at else None
            })
    
    # Get latest batch job for summary
    job = BatchJob.query.filter_by(class_id=class_id, status='completed').order_by(BatchJob.completed_at.desc()).first()
    
    class_summary = None
    if job and job.class_summary:
        class_summary = json.loads(job.class_summary)
    
    return jsonify({
        'children': results,
        'class_summary': class_summary
    })


@nursery_bp.route('/children/<child_id>/results', methods=['GET'])
def get_child_results(child_id):
    """Get analysis results for a single child"""
    
    child = Child.query.get(child_id)
    
    if not child or not child.assessment:
        return jsonify({'error': 'Child not found'}), 404
    
    a = child.assessment
    
    return jsonify({
        'id': child.id,
        'name': child.first_name,
        'age': child.age_string,
        'score': a.score,
        'stage': a.stage,
        'analysis': json.loads(a.analysis_result or '{}'),
        'observations': {
            'verbal': json.loads(a.observations_verbal or '[]'),
            'paper': json.loads(a.observations_paper or '[]'),
            'difficulty': json.loads(a.observations_difficulty or '[]')
        },
        'status': a.status,
        'analysed_at': a.analysed_at.isoformat() if a.analysed_at else None
    })
