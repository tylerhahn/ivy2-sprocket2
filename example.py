from ivy2 import Ivy2Printer
import image
from flask import Flask, request, jsonify
import os
import base64
from werkzeug.utils import secure_filename
import requests

app = Flask(__name__)

PRINTER_MAC = "XX:XX:XX:XX:XX:XX"
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}
PI_ADDRESS = "192.168.1.63:5000"

# Create uploads directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def print_shrek():
    printer = Ivy2Printer()
    printer.connect(PRINTER_MAC)

    printer.print("./assets/test_image.jpg")

    printer.disconnect()

def preview_image(image_path, output_path="preview_image.jpeg"):
    """Get a preview of what the printed image will look like."""

    image_data = image.prepare_image(image_path, True, 100, True)

    with open(output_path, "wb") as file:
        file.write(image_data)

@app.route('/print', methods=['POST'])
def print_photo():
    """Endpoint to print a photo sent via HTTP request."""
    try:
        # Check if image file is in the request
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            # Print the image
            printer = Ivy2Printer()
            printer.connect(PRINTER_MAC)
            printer.print(filepath)
            printer.disconnect()

            # Clean up the uploaded file
            os.remove(filepath)

            return jsonify({'message': 'Photo printed successfully', 'filename': filename}), 200
        else:
            return jsonify({'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, bmp'}), 400

    except Exception as e:
        return jsonify({'error': f'Printing failed: {str(e)}'}), 500

@app.route('/print/pi', methods=['POST'])
def print_photo_to_pi():
    """Forward print request to Raspberry Pi."""
    try:
        # Check if image file is in the request
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if file and allowed_file(file.filename):
            # Forward the file to the Pi
            files = {'image': (file.filename, file.read(), file.content_type)}
            response = requests.post(f'http://{PI_ADDRESS}/print', files=files)

            return jsonify(response.json()), response.status_code
        else:
            return jsonify({'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, bmp'}), 400

    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Raspberry Pi. Make sure it\'s running the print server.'}), 503
    except Exception as e:
        return jsonify({'error': f'Printing failed: {str(e)}'}), 500

@app.route('/print/base64', methods=['POST'])
def print_photo_base64():
    """Endpoint to print a photo sent as base64 encoded data."""
    try:
        data = request.get_json()
        if not data or 'image_data' not in data:
            return jsonify({'error': 'No image data provided'}), 400

        # Decode base64 image data
        image_data = base64.b64decode(data['image_data'])

        # Save temporarily
        temp_filename = 'temp_image.jpg'
        temp_filepath = os.path.join(UPLOAD_FOLDER, temp_filename)

        with open(temp_filepath, 'wb') as f:
            f.write(image_data)

        # Print the image
        printer = Ivy2Printer()
        printer.connect(PRINTER_MAC)
        printer.print(temp_filepath)
        printer.disconnect()

        # Clean up
        os.remove(temp_filepath)

        return jsonify({'message': 'Photo printed successfully'}), 200

    except Exception as e:
        return jsonify({'error': f'Printing failed: {str(e)}'}), 500

@app.route('/print/base64/pi', methods=['POST'])
def print_photo_base64_to_pi():
    """Forward base64 print request to Raspberry Pi."""
    try:
        data = request.get_json()
        if not data or 'image_data' not in data:
            return jsonify({'error': 'No image data provided'}), 400

        # Forward the request to the Pi
        response = requests.post(f'http://{PI_ADDRESS}/print/base64', json=data)

        return jsonify(response.json()), response.status_code

    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Raspberry Pi. Make sure it\'s running the print server.'}), 503
    except Exception as e:
        return jsonify({'error': f'Printing failed: {str(e)}'}), 500

@app.route('/status', methods=['GET'])
def printer_status():
    """Get printer status."""
    try:
        printer = Ivy2Printer()
        printer.connect(PRINTER_MAC)
        status = printer.get_status()
        printer.disconnect()

        return jsonify({
            'connected': True,
            'status': status
        }), 200

    except Exception as e:
        return jsonify({
            'connected': False,
            'error': str(e)
        }), 500

@app.route('/status/pi', methods=['GET'])
def pi_status():
    """Get Raspberry Pi printer status."""
    try:
        response = requests.get(f'http://{PI_ADDRESS}/status')
        return jsonify(response.json()), response.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Raspberry Pi'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({'status': 'healthy', 'service': 'ivy2-printer-api'}), 200

if __name__ == '__main__':
    # Run the Flask app
    print("Starting Ivy2 Printer API server...")
    print("Available endpoints:")
    print("  POST /print - Upload and print an image file (local)")
    print("  POST /print/pi - Upload and print an image file (via Pi)")
    print("  POST /print/base64 - Print base64 encoded image (local)")
    print("  POST /print/base64/pi - Print base64 encoded image (via Pi)")
    print("  GET /status - Get printer status (local)")
    print("  GET /status/pi - Get printer status (via Pi)")
    print("  GET /health - Health check")
    print(f"\nPi address: {PI_ADDRESS}")
    print("\nServer will start on http://localhost:5000")

    app.run(host='0.0.0.0', port=5000, debug=True)
