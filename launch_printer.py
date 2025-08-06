from ivy2 import Ivy2Printer
import image
from flask import Flask, request, jsonify
import os
import base64
from werkzeug.utils import secure_filename
import requests
import threading
import queue
import time
import uuid
from datetime import datetime
import json

app = Flask(__name__)

PRINTER_MAC = "10:23:81:44:C1:CD"
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}
PI_ADDRESS = "192.168.1.63:5000"

# Create uploads directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Print queue and job management
print_queue = queue.Queue()
job_status = {}  # job_id -> status dict
job_lock = threading.Lock()

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def create_job_id():
    """Generate a unique job ID."""
    return str(uuid.uuid4())

def update_job_status(job_id, status, message="", progress=0):
    """Update job status in a thread-safe way."""
    with job_lock:
        job_status[job_id] = {
            'status': status,  # 'queued', 'processing', 'completed', 'failed'
            'message': message,
            'progress': progress,
            'timestamp': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }

def get_job_status(job_id):
    """Get job status in a thread-safe way."""
    with job_lock:
        return job_status.get(job_id, None)

def get_all_jobs():
    """Get all jobs in a thread-safe way."""
    with job_lock:
        return {job_id: job_data.copy() for job_id, job_data in job_status.items()}

def create_printer_instance():
    """Create a new printer instance with its own thread."""
    return Ivy2Printer()

def print_worker():
    """Background worker that processes print jobs from the queue."""
    while True:
        try:
            # Get job from queue (blocking)
            job_data = print_queue.get(timeout=1)
            job_id = job_data['job_id']
            filepath = job_data['filepath']
            filename = job_data['filename']

            update_job_status(job_id, 'processing', 'Connecting to printer...', 10)

            # Create a new printer instance for each job
            printer = create_printer_instance()

            try:
                # Connect to printer
                update_job_status(job_id, 'processing', 'Connecting to printer...', 20)
                printer.connect(PRINTER_MAC)

                # Check printer status
                update_job_status(job_id, 'processing', 'Checking printer status...', 30)
                status = printer.get_status()
                error_code, battery_level, _, is_cover_open, is_no_paper, is_wrong_smart_sheet = status

                if battery_level < 10:
                    raise Exception("Printer battery too low")
                if is_cover_open:
                    raise Exception("Printer cover is open")
                if is_no_paper:
                    raise Exception("No paper in printer")
                if is_wrong_smart_sheet:
                    raise Exception("Wrong smart sheet detected")

                # Print the image
                update_job_status(job_id, 'processing', 'Printing image...', 50)
                printer.print(filepath)

                # Clean up
                if os.path.exists(filepath):
                    os.remove(filepath)

                update_job_status(job_id, 'completed', 'Print job completed successfully', 100)

            except Exception as e:
                # Clean up file even if printing fails
                if os.path.exists(filepath):
                    os.remove(filepath)
                update_job_status(job_id, 'failed', f'Print failed: {str(e)}', 0)
            finally:
                # Always disconnect the printer
                try:
                    printer.disconnect()
                except:
                    pass  # Ignore disconnect errors

        except queue.Empty:
            # No jobs in queue, continue waiting
            continue
        except Exception as e:
            # Handle any other errors in the worker
            if 'job_id' in locals():
                update_job_status(job_id, 'failed', f'Worker error: {str(e)}', 0)
            continue

def start_print_worker():
    """Start the background print worker thread."""
    worker_thread = threading.Thread(target=print_worker, daemon=True)
    worker_thread.start()
    return worker_thread

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

def handle_printer_error(e):
    """Handle printer-specific errors and return appropriate error messages."""
    error_type = type(e).__name__

    if error_type == 'LowBatteryError':
        return jsonify({
            'error': 'Printer battery is too low to print. Please charge the printer.',
            'error_type': 'low_battery',
            'suggestion': 'Charge the printer and try again.'
        }), 503
    elif error_type == 'CoverOpenError':
        return jsonify({
            'error': 'Printer cover is open. Please close the cover.',
            'error_type': 'cover_open',
            'suggestion': 'Close the printer cover and try again.'
        }), 503
    elif error_type == 'NoPaperError':
        return jsonify({
            'error': 'No paper in the printer. Please add paper.',
            'error_type': 'no_paper',
            'suggestion': 'Add paper to the printer and try again.'
        }), 503
    elif error_type == 'WrongSmartSheetError':
        return jsonify({
            'error': 'Wrong smart sheet detected. Please use the correct sheet.',
            'error_type': 'wrong_sheet',
            'suggestion': 'Use the correct smart sheet for your printer.'
        }), 503
    elif error_type == 'ClientUnavailableError':
        return jsonify({
            'error': 'Printer is not connected or unavailable.',
            'error_type': 'connection_error',
            'suggestion': 'Check that the printer is turned on and connected.'
        }), 503
    elif error_type == 'ReceiveTimeoutError':
        return jsonify({
            'error': 'Printer communication timeout.',
            'error_type': 'timeout',
            'suggestion': 'Try again or check printer connection.'
        }), 503
    else:
        return jsonify({
            'error': f'Printer error: {str(e)}',
            'error_type': 'unknown_error'
        }), 500

@app.route('/print', methods=['POST'])
def print_photo():
    """Endpoint to print a photo sent via HTTP request (queued)."""
    try:
        # Check if image file is in the request
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if file and allowed_file(file.filename):
            # Generate job ID
            job_id = create_job_id()
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, f"{job_id}_{filename}")

            # Save file
            file.save(filepath)

            # Create job data
            job_data = {
                'job_id': job_id,
                'filepath': filepath,
                'filename': filename,
                'created_at': datetime.now().isoformat()
            }

            # Add to queue
            print_queue.put(job_data)

            # Initialize job status
            update_job_status(job_id, 'queued', 'Job added to print queue', 0)

            return jsonify({
                'message': 'Print job queued successfully',
                'job_id': job_id,
                'filename': filename,
                'queue_position': print_queue.qsize()
            }), 202
        else:
            return jsonify({'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, bmp'}), 400

    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@app.route('/print/immediate', methods=['POST'])
def print_photo_immediate():
    """Endpoint to print a photo immediately (no queue)."""
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

            # Create a new printer instance
            printer = create_printer_instance()

            try:
                # Print the image immediately
                printer.connect(PRINTER_MAC)
                printer.print(filepath)
                printer.disconnect()

                # Clean up the uploaded file
                os.remove(filepath)

                return jsonify({'message': 'Photo printed successfully', 'filename': filename}), 200

            except Exception as e:
                # Clean up the uploaded file even if printing fails
                if os.path.exists(filepath):
                    os.remove(filepath)
                return handle_printer_error(e)
            finally:
                # Always disconnect
                try:
                    printer.disconnect()
                except:
                    pass
        else:
            return jsonify({'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, bmp'}), 400

    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

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
    """Endpoint to print a photo sent as base64 encoded data (queued)."""
    try:
        data = request.get_json()
        if not data or 'image_data' not in data:
            return jsonify({'error': 'No image data provided'}), 400

        # Generate job ID
        job_id = create_job_id()

        # Decode base64 image data
        image_data = base64.b64decode(data['image_data'])

        # Save temporarily
        temp_filename = f"{job_id}_temp_image.jpg"
        temp_filepath = os.path.join(UPLOAD_FOLDER, temp_filename)

        with open(temp_filepath, 'wb') as f:
            f.write(image_data)

        # Create job data
        job_data = {
            'job_id': job_id,
            'filepath': temp_filepath,
            'filename': 'base64_image.jpg',
            'created_at': datetime.now().isoformat()
        }

        # Add to queue
        print_queue.put(job_data)

        # Initialize job status
        update_job_status(job_id, 'queued', 'Job added to print queue', 0)

        return jsonify({
            'message': 'Print job queued successfully',
            'job_id': job_id,
            'queue_position': print_queue.qsize()
        }), 202

    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

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

@app.route('/jobs', methods=['GET'])
def list_jobs():
    """List all print jobs."""
    try:
        jobs = get_all_jobs()

        # Add queue information
        queue_size = print_queue.qsize()

        return jsonify({
            'jobs': jobs,
            'queue_size': queue_size,
            'total_jobs': len(jobs)
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to get jobs: {str(e)}'}), 500

@app.route('/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    """Get specific job status."""
    try:
        job = get_job_status(job_id)
        if job is None:
            return jsonify({'error': 'Job not found'}), 404

        return jsonify({
            'job_id': job_id,
            'status': job
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to get job: {str(e)}'}), 500

@app.route('/jobs/<job_id>', methods=['DELETE'])
def cancel_job(job_id):
    """Cancel a print job (if it's still queued)."""
    try:
        job = get_job_status(job_id)
        if job is None:
            return jsonify({'error': 'Job not found'}), 404

        if job['status'] == 'queued':
            # Note: We can't easily remove from queue, but we can mark it as cancelled
            update_job_status(job_id, 'cancelled', 'Job cancelled by user', 0)
            return jsonify({'message': 'Job cancelled successfully'}), 200
        else:
            return jsonify({'error': 'Cannot cancel job that is already processing or completed'}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to cancel job: {str(e)}'}), 500

@app.route('/queue/clear', methods=['POST'])
def clear_queue():
    """Clear all queued jobs."""
    try:
        # Clear the queue
        while not print_queue.empty():
            try:
                print_queue.get_nowait()
            except queue.Empty:
                break

        # Mark all queued jobs as cancelled
        with job_lock:
            for job_id, job_data in job_status.items():
                if job_data['status'] == 'queued':
                    job_data['status'] = 'cancelled'
                    job_data['message'] = 'Job cancelled due to queue clear'
                    job_data['updated_at'] = datetime.now().isoformat()

        return jsonify({'message': 'Queue cleared successfully'}), 200
    except Exception as e:
        return jsonify({'error': f'Failed to clear queue: {str(e)}'}), 500

@app.route('/status', methods=['GET'])
def printer_status():
    """Get printer status."""
    try:
        printer = create_printer_instance()
        printer.connect(PRINTER_MAC)
        status = printer.get_status()
        printer.disconnect()

        # Parse status for better response
        error_code, battery_level, _, is_cover_open, is_no_paper, is_wrong_smart_sheet = status

        return jsonify({
            'connected': True,
            'status': {
                'error_code': error_code,
                'battery_level': battery_level,
                'cover_open': is_cover_open,
                'no_paper': is_no_paper,
                'wrong_smart_sheet': is_wrong_smart_sheet,
                'can_print': battery_level >= 10 and not is_cover_open and not is_no_paper and not is_wrong_smart_sheet
            }
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

@app.route('/settings', methods=['GET'])
def get_printer_settings():
    """Get current printer settings."""
    try:
        printer = create_printer_instance()
        printer.connect(PRINTER_MAC)
        settings = printer.get_setting()
        printer.disconnect()

        return jsonify({
            'connected': True,
            'settings': settings
        }), 200

    except Exception as e:
        return jsonify({
            'connected': False,
            'error': str(e)
        }), 500

@app.route('/settings/auto-power-off', methods=['POST'])
def set_auto_power_off():
    """Set the auto power-off setting on the printer."""
    try:
        data = request.get_json()
        if not data or 'minutes' not in data:
            return jsonify({'error': 'Minutes parameter required'}), 400

        minutes = data['minutes']

        # Validate supported values
        if minutes not in [3, 5, 10]:
            return jsonify({
                'error': 'Invalid minutes value. Supported values: 3, 5, 10',
                'supported_values': [3, 5, 10]
            }), 400

        printer = create_printer_instance()
        printer.connect(PRINTER_MAC)
        result = printer.set_setting(minutes)
        printer.disconnect()

        return jsonify({
            'message': f'Auto power-off set to {minutes} minutes',
            'setting': minutes,
            'result': result
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to set auto power-off: {str(e)}'}), 500

@app.route('/settings/keep-on', methods=['POST'])
def keep_printer_on():
    """Set printer to stay on (maximum auto power-off time)."""
    try:
        printer = create_printer_instance()
        printer.connect(PRINTER_MAC)

        # Set to maximum time (10 minutes) to keep it on as long as possible
        result = printer.set_setting(10)
        printer.disconnect()

        return jsonify({
            'message': 'Printer set to stay on for maximum time (10 minutes)',
            'setting': 10,
            'note': 'Printer will still auto-off after 10 minutes of inactivity. For continuous operation, send periodic status checks or print jobs.'
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to set printer to stay on: {str(e)}'}), 500

@app.route('/settings/disable-auto-off', methods=['POST'])
def disable_auto_power_off():
    """Attempt to disable auto power-off (if supported)."""
    try:
        printer = create_printer_instance()
        printer.connect(PRINTER_MAC)

        # Try to set to maximum time (10 minutes)
        result = printer.set_setting(10)
        printer.disconnect()

        return jsonify({
            'message': 'Auto power-off set to maximum time (10 minutes)',
            'setting': 10,
            'note': 'The Ivy 2 printer does not support completely disabling auto power-off. It will still turn off after 10 minutes of inactivity. To keep it on continuously, send periodic status checks or print jobs.',
            'recommendation': 'Use /keep-alive endpoint to send periodic status checks'
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to configure auto power-off: {str(e)}'}), 500

@app.route('/keep-alive', methods=['POST'])
def keep_alive():
    """Send a keep-alive signal to prevent printer from turning off."""
    try:
        printer = create_printer_instance()
        printer.connect(PRINTER_MAC)

        # Just get status to keep the connection alive
        status = printer.get_status()
        printer.disconnect()

        return jsonify({
            'message': 'Keep-alive signal sent successfully',
            'status': 'printer_awake',
            'timestamp': datetime.now().isoformat()
        }), 200

    except Exception as e:
        return jsonify({
            'error': f'Keep-alive failed: {str(e)}',
            'status': 'printer_unavailable'
        }), 500

@app.route('/keep-alive/start', methods=['POST'])
def start_keep_alive():
    """Start automatic keep-alive service."""
    try:
        data = request.get_json() or {}
        interval = data.get('interval', 300)  # Default 5 minutes

        # Start background keep-alive thread
        def keep_alive_worker():
            while True:
                try:
                    printer = create_printer_instance()
                    printer.connect(PRINTER_MAC)
                    printer.get_status()  # Just ping the printer
                    printer.disconnect()
                    time.sleep(interval)
                except Exception as e:
                    print(f"Keep-alive error: {e}")
                    time.sleep(60)  # Wait 1 minute before retry

        keep_alive_thread = threading.Thread(target=keep_alive_worker, daemon=True)
        keep_alive_thread.start()

        return jsonify({
            'message': f'Keep-alive service started with {interval} second intervals',
            'interval_seconds': interval,
            'status': 'keep_alive_active'
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to start keep-alive: {str(e)}'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'service': 'ivy2-printer-api',
        'queue_size': print_queue.qsize(),
        'active_jobs': len([j for j in job_status.values() if j['status'] in ['queued', 'processing']])
    }), 200

if __name__ == '__main__':
    # Start the print worker
    print("Starting print worker thread...")
    start_print_worker()

    # Run the Flask app
    print("Starting Ivy2 Printer API server...")
    print("Available endpoints:")
    print("  POST /print - Upload and print an image file (queued)")
    print("  POST /print/immediate - Upload and print an image file (immediate)")
    print("  POST /print/pi - Upload and print an image file (via Pi)")
    print("  POST /print/base64 - Print base64 encoded image (queued)")
    print("  POST /print/base64/pi - Print base64 encoded image (via Pi)")
    print("  GET /jobs - List all print jobs")
    print("  GET /jobs/<job_id> - Get specific job status")
    print("  DELETE /jobs/<job_id> - Cancel a job")
    print("  POST /queue/clear - Clear all queued jobs")
    print("  GET /status - Get printer status (local)")
    print("  GET /status/pi - Get printer status (via Pi)")
    print("  GET /settings - Get printer settings")
    print("  POST /settings/auto-power-off - Set auto power-off time")
    print("  POST /settings/keep-on - Set printer to stay on")
    print("  POST /settings/disable-auto-off - Disable auto power-off")
    print("  POST /keep-alive - Send keep-alive signal")
    print("  POST /keep-alive/start - Start automatic keep-alive service")
    print("  GET /health - Health check")
    print(f"\nPi address: {PI_ADDRESS}")
    print("\nServer will start on http://localhost:5000")

    app.run(host='0.0.0.0', port=5000, debug=True)
