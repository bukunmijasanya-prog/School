"""
Nursery Extension - Main Flask Application
"""

from flask import Flask, render_template, redirect, url_for
import os

from .models import db
from .routes import nursery_bp


def create_app():
    """Create and configure the Flask application"""
    
    app = Flask(__name__, template_folder='../templates')
    
    # Configuration
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-in-production')
    
    # Database configuration
    database_url = os.getenv('DATABASE_URL')
    if database_url:
        # Azure PostgreSQL
        app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    else:
        # Local SQLite for development
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///nursery.db'
    
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # Initialize database
    db.init_app(app)
    
    # Register blueprints
    app.register_blueprint(nursery_bp)
    
    # Create tables
    with app.app_context():
        db.create_all()
    
    # Home route
    @app.route('/')
    def home():
        return render_template('index.html')
    
    # Health check for Azure
    @app.route('/health')
    def health():
        return {'status': 'healthy'}, 200
    
    return app


# For running locally
if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, port=5000)
