import os
from nursery.app import create_app

# Create the Flask application
app = create_app()

if __name__ == '__main__':
    # Get port from environment (Azure sets this) or default to 5000
    port = int(os.getenv('PORT', 5000))
    
    # Run the app
    app.run(host='0.0.0.0', port=port)
```
