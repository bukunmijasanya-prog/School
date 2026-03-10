"""
Database models for the Nursery Extension
"""

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid

db = SQLAlchemy()


def generate_uuid():
    return str(uuid.uuid4())


class School(db.Model):
    """A nursery school account"""
    __tablename__ = 'schools'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    teachers = db.relationship('Teacher', backref='school', lazy=True)
    classes = db.relationship('SchoolClass', backref='school', lazy=True)


class Teacher(db.Model):
    """A teacher who uses the system"""
    __tablename__ = 'teachers'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    school_id = db.Column(db.String(36), db.ForeignKey('schools.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SchoolClass(db.Model):
    """A class of children (e.g., 'Reception 2024')"""
    __tablename__ = 'classes'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    school_id = db.Column(db.String(36), db.ForeignKey('schools.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    academic_year = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    children = db.relationship('Child', backref='school_class', lazy=True)


class Child(db.Model):
    """A child in a class"""
    __tablename__ = 'children'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    class_id = db.Column(db.String(36), db.ForeignKey('classes.id'), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    assessment = db.relationship('Assessment', backref='child', uselist=False)
    
    @property
    def age_string(self):
        """Return age as '3 years 4 months' format"""
        today = datetime.utcnow().date()
        years = today.year - self.date_of_birth.year
        months = today.month - self.date_of_birth.month
        
        if months < 0:
            years -= 1
            months += 12
            
        return f"{years} years {months} months"


class Assessment(db.Model):
    """Assessment sheet and results for a child"""
    __tablename__ = 'assessments'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    child_id = db.Column(db.String(36), db.ForeignKey('children.id'), nullable=False)
    
    # Image storage
    image_filename = db.Column(db.String(255))
    image_url = db.Column(db.String(500))
    
    # Teacher observations (stored as JSON strings)
    observations_verbal = db.Column(db.Text)  # JSON array
    observations_paper = db.Column(db.Text)   # JSON array
    observations_difficulty = db.Column(db.Text)  # JSON array
    
    # AI Analysis results
    score = db.Column(db.Integer)  # 1-5
    stage = db.Column(db.String(50))  # pre-differentiation, emerging, developing, established
    analysis_result = db.Column(db.Text)  # Full JSON response from AI
    
    # Status
    status = db.Column(db.String(20), default='pending')  # pending, analysed, error
    
    # Timestamps
    uploaded_at = db.Column(db.DateTime)
    analysed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class BatchJob(db.Model):
    """Tracks batch analysis jobs for a class"""
    __tablename__ = 'batch_jobs'
    
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    class_id = db.Column(db.String(36), db.ForeignKey('classes.id'), nullable=False)
    
    # Job status
    status = db.Column(db.String(20), default='pending')  # pending, processing, completed, failed
    total_children = db.Column(db.Integer, default=0)
    processed_children = db.Column(db.Integer, default=0)
    
    # Results summary
    class_summary = db.Column(db.Text)  # JSON summary
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    
    # Error tracking
    error_message = db.Column(db.Text)
