from ivy2 import Ivy2Printer
import image
from flask import Flask, request, jsonify
import os
import base64
from werkzeug.utils import secure_filename
import requests
import threading
import time
import uuid
from datetime import datetime
import json
from loguru import logger
from exceptions import ReceiveTimeoutError

# Try to load .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("WARNING: python-dotenv not installed. Install with: pip install python-dotenv")
    print("WARNING: Falling back to environment variables or defaults")

app = Flask(__name__)

# =========================
# Printer & server config
# =========================
# Load printer MACs from .env file or environment variables
# Format: PRINTER_MACS=AA:BB:CC:DD:EE:FF,11:22:33:44:55:66
_printer_macs_env = os.getenv('PRINTER_MACS', '').strip()

if _printer_macs_env:
    # Parse comma-separated MAC addresses
    PRINTER_MACS = [mac.strip() for mac in _printer_macs_env.split(',') if mac.strip()]
    logger.info(f"Loaded {len(PRINTER_MACS)} printer MAC(s) from environment: {PRINTER_MACS}")
else:
    # Fallback to default if not set in .env
    PRINTER_MACS = [
        "10:23:81:44:C1:CD",  # Printer A
        "10:23:81:44:C1:CE",  # Printer B
    ]
    logger.warning(f"No PRINTER_MACS found in .env, using defaults: {PRINTER_MACS}")
    logger.info("To configure printers, create a .env file with: PRINTER_MACS=AA:BB:CC:DD:EE:FF,11:22:33:44:55:66")

if not PRINTER_MACS:
    raise ValueError("No printer MAC addresses configured! Set PRINTER_MACS in .env file or environment variable.")

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}
PI_ADDRESS = os.getenv('PI_ADDRESS', "192.168.1.55:5000")

# Create uploads directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========================
# Active print tracking (max 2, one per printer)
# =========================
active_prints = {}     # mac -> job_id (tracks which printer is handling which job)
active_prints_lock = threading.Lock()

job_status = {}        # job_id -> status dict
job_lock = threading.Lock()

# Keep-alive per-printer threads
keep_alive_threads = {}   # mac -> Thread


# =========================
# Utilities
# =========================
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def create_job_id():
    """Generate a unique job ID."""
    return str(uuid.uuid4())


def update_job_status(job_id, status, message="", progress=0, extra=None):
    """Update job status in a thread-safe way."""
    with job_lock:
        job_status[job_id] = {
            'status': status,  # 'processing', 'completed', 'failed'
            'message': message,
            'progress': progress,
            'timestamp': job_status.get(job_id, {}).get('timestamp', datetime.now().isoformat()),
            'updated_at': datetime.now().isoformat(),
            **(extra or {})
        }


def get_active_print_count():
    """Get number of currently active prints."""
    with active_prints_lock:
        return len(active_prints)


def get_available_printer():
    """
    Get an available printer MAC, or None if both are busy/unavailable.
    Returns: (mac_address, None) if available, or (None, error_details) if unavailable.
    error_details is a dict with 'error' and 'reason' keys.
    """
    unavailable_reasons = []

    with active_prints_lock:
        for mac in PRINTER_MACS:
            if mac not in active_prints:
                # Check if printer is actually ready
                try:
                    ready, message = check_printer_ready(mac)
                    if ready:
                        return mac, None
                    else:
                        # Get detailed status to understand why not ready
                        ok, detail = safe_connect_status(mac)
                        if ok and isinstance(detail, dict):
                            reason_parts = []
                            if detail.get('no_paper'):
                                reason_parts.append("no paper")
                            if detail.get('cover_open'):
                                reason_parts.append("cover open")
                            if detail.get('wrong_smart_sheet'):
                                reason_parts.append("wrong smart sheet")
                            if detail.get('battery_level', 100) < 10:
                                reason_parts.append("low battery")
                            reason = ", ".join(reason_parts) if reason_parts else message
                            unavailable_reasons.append({
                                'mac': mac,
                                'reason': reason
                            })
                        else:
                            unavailable_reasons.append({
                                'mac': mac,
                                'reason': detail if isinstance(detail, str) else "unavailable"
                            })
                except Exception as e:
                    # If printer check fails (timeout, connection error, etc.), skip it
                    # and try the next printer
                    logger.debug(f"Printer {mac} check failed: {e}")
                    unavailable_reasons.append({
                        'mac': mac,
                        'reason': f"connection error: {str(e)}"
                    })
                    continue

    # All printers are unavailable - return detailed error
    if unavailable_reasons:
        # Check if all have the same issue (like "no paper")
        reasons = [r['reason'] for r in unavailable_reasons]
        if all('no paper' in r.lower() for r in reasons):
            return None, {
                'error': 'All printers have no paper',
                'error_type': 'no_paper',
                'printers': unavailable_reasons,
                'suggestion': 'Please add paper to the printers and try again.'
            }
        elif all('cover open' in r.lower() for r in reasons):
            return None, {
                'error': 'All printer covers are open',
                'error_type': 'cover_open',
                'printers': unavailable_reasons,
                'suggestion': 'Please close the printer covers and try again.'
            }
        elif all('low battery' in r.lower() for r in reasons):
            return None, {
                'error': 'All printers have low battery',
                'error_type': 'low_battery',
                'printers': unavailable_reasons,
                'suggestion': 'Please charge the printers and try again.'
            }

    # Mixed or other reasons
    return None, {
        'error': 'All printers are currently unavailable',
        'error_type': 'printers_unavailable',
        'printers': unavailable_reasons,
        'suggestion': 'Please check the printer status and try again.'
    }


def start_print(job_id, printer_mac, filepath, filename):
    """Start a print job on a specific printer in a background thread."""
    # Mark printer as active immediately (before starting thread)
    with active_prints_lock:
        active_prints[printer_mac] = job_id

    def print_worker():
        try:
            update_job_status(job_id, 'processing', f'Connecting to printer {printer_mac}...', 15,
                            extra={'printer_mac': printer_mac})

            printer = create_printer_instance()
            try:
                printer.connect(printer_mac)

                update_job_status(job_id, 'processing', 'Checking printer status...', 30)
                status = printer.get_status()
                error_code, battery_level, _, is_cover_open, is_no_paper, is_wrong_smart_sheet = status

                if battery_level < 10:
                    raise Exception("Printer battery too low")
                if is_cover_open:
                    raise Exception("Printer cover is open")
                if is_no_paper:
                    error_msg = f"No paper in printer {printer_mac}. Please add paper and try again."
                    logger.error(f"Print job {job_id}: {error_msg}")
                    update_job_status(job_id, 'failed', error_msg, 0)
                    raise Exception(error_msg)
                if is_wrong_smart_sheet:
                    error_msg = f"Wrong smart sheet in printer {printer_mac}. Please use the correct sheet."
                    logger.error(f"Print job {job_id}: {error_msg}")
                    update_job_status(job_id, 'failed', error_msg, 0)
                    raise Exception(error_msg)

                update_job_status(job_id, 'processing', 'Printing image...', 60)

                # Calculate a more generous timeout based on file size
                file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                # Estimate: ~0.02s per 990 byte chunk + processing time
                # Use at least 120 seconds, or 2x estimated time
                estimated_chunks = (file_size + 989) // 990
                estimated_time = max(120, estimated_chunks * 0.02 * 3 + 30)  # 3x safety factor
                transfer_timeout = int(estimated_time)

                logger.debug(f"Print job {job_id}: file_size={file_size}, estimated_time={estimated_time}s, timeout={transfer_timeout}s")

                printer.print(filepath, transfer_timeout=transfer_timeout)

                # Data transfer is complete - this is the critical success point
                # The printer has received all the data and will print it
                logger.info(f"Print job {job_id}: Data transfer complete. Printer has received image data.")

                # Data transfer is complete, but printer might still be physically printing
                update_job_status(job_id, 'processing', 'Waiting for print to complete...', 80)

                # Wait for the printer to actually finish printing
                # Poll status to ensure printer is ready before marking as complete
                # If this fails or times out, we still consider the job successful since data was sent
                try:
                    print_complete = printer.wait_for_print_complete(max_wait_time=180, poll_interval=2)

                    if not print_complete:
                        logger.warning(f"Print job {job_id}: Timeout waiting for print completion, but data was sent successfully")
                except Exception as wait_error:
                    # Don't fail the job if waiting for completion fails
                    # The data was already successfully transferred to the printer
                    logger.warning(f"Print job {job_id}: Error during wait_for_print_complete: {wait_error}. "
                                 f"Data was already sent successfully, marking job as completed.")
                    print_complete = True  # Consider it complete since data was sent

                # Clean up file
                if os.path.exists(filepath):
                    os.remove(filepath)

                # Mark as completed - data transfer was successful, which is what matters
                update_job_status(job_id, 'completed', f'Printed on {printer_mac}', 100)

            except ReceiveTimeoutError as e:
                # Clean up file even if printing fails
                if os.path.exists(filepath):
                    os.remove(filepath)
                error_msg = f'Print timeout on {printer_mac}. The printer may be slow or the image too large. Try a smaller image or check printer connection.'
                logger.error(f"Print timeout for job {job_id}: {e}")
                update_job_status(job_id, 'failed', error_msg, 0)
            except Exception as e:
                # Clean up file even if printing fails
                if os.path.exists(filepath):
                    os.remove(filepath)
                error_type = type(e).__name__
                error_msg = f'Print failed on {printer_mac}: {str(e)}'
                logger.error(f"Print error for job {job_id} ({error_type}): {e}")
                update_job_status(job_id, 'failed', error_msg, 0)
            finally:
                try:
                    printer.disconnect()
                except:
                    pass
                # Remove from active prints
                with active_prints_lock:
                    active_prints.pop(printer_mac, None)

        except Exception as e:
            update_job_status(job_id, 'failed', f'Print error: {str(e)}', 0)
            with active_prints_lock:
                active_prints.pop(printer_mac, None)

    thread = threading.Thread(target=print_worker, daemon=True)
    thread.start()


def get_job_status(job_id):
    """Get job status in a thread-safe way."""
    with job_lock:
        return job_status.get(job_id, None)


def get_all_jobs():
    """Get all jobs in a thread-safe way."""
    with job_lock:
        return {job_id: job_data.copy() for job_id, job_data in job_status.items()}


def create_printer_instance():
    """Create a new printer instance with its own connection."""
    return Ivy2Printer()


# =========================
# Printer helpers (multi)
# =========================
def safe_connect_status(printer_mac):
    """
    Try to connect and read status quickly. Return (ok, detail|error_msg).
    Detail example:
      {
        'error_code': int,
        'battery_level': int,
        'cover_open': bool,
        'no_paper': bool,
        'wrong_smart_sheet': bool,
        'can_print': bool
      }
    """
    printer = None
    try:
        printer = create_printer_instance()
        printer.connect(printer_mac)
        status = printer.get_status()
        error_code, battery_level, _, is_cover_open, is_no_paper, is_wrong_smart_sheet = status
        can_print = battery_level >= 10 and not is_cover_open and not is_no_paper and not is_wrong_smart_sheet
        detail = {
            'error_code': error_code,
            'battery_level': battery_level,
            'cover_open': is_cover_open,
            'no_paper': is_no_paper,
            'wrong_smart_sheet': is_wrong_smart_sheet,
            'can_print': can_print
        }
        return True, detail
    except ReceiveTimeoutError as e:
        logger.debug(f"Timeout connecting to printer {printer_mac}: {e}")
        return False, f"Connection timeout: {str(e)}"
    except Exception as e:
        logger.debug(f"Error connecting to printer {printer_mac}: {e}")
        return False, str(e)
    finally:
        if printer:
            try:
                printer.disconnect()
            except Exception as e:
                logger.debug(f"Error disconnecting printer {printer_mac}: {e}")
                pass


def check_printer_ready(printer_mac):
    """Check if a printer is ready to print. Returns (ready: bool, message: str)."""
    try:
        ok, detail = safe_connect_status(printer_mac)
        if not ok:
            return False, f"Unavailable: {detail}"
        can_print = detail.get('can_print', False) if isinstance(detail, dict) else False
        return can_print, "Ready" if can_print else "Not ready"
    except Exception as e:
        logger.debug(f"Error checking printer {printer_mac} readiness: {e}")
        return False, f"Error: {str(e)}"


def choose_available_printer():
    """Return the first MAC that is currently ready; else None."""
    for mac in PRINTER_MACS:
        ready, _ = check_printer_ready(mac)
        if ready:
            return mac
    return None




# =========================
# Optional utilities
# =========================
def print_shrek():
    """Simple local test function (update MAC/path as needed)."""
    printer = Ivy2Printer()
    printer.connect(PRINTER_MACS[0])
    printer.print("./assets/test_image.jpg")
    printer.disconnect()


def preview_image(image_path, output_path="preview_image.jpeg"):
    """Get a preview of what the printed image will look like."""
    image_data = image.prepare_image(image_path, True, 100, True)
    with open(output_path, "wb") as file:
        file.write(image_data)


def handle_printer_error(e):
    """Map specific printer exceptions to user-friendly JSON responses."""
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


# =========================
# SETTINGS helpers (multi)
# =========================
def get_settings_for_mac(mac):
    printer = create_printer_instance()
    try:
        printer.connect(mac)
        s = printer.get_setting()
        return True, s
    except Exception as e:
        return False, str(e)
    finally:
        try:
            printer.disconnect()
        except:
            pass


def set_auto_power_off_for_mac(mac, minutes):
    printer = create_printer_instance()
    try:
        printer.connect(mac)
        result = printer.set_setting(minutes)
        return True, result
    except Exception as e:
        return False, str(e)
    finally:
        try:
            printer.disconnect()
        except:
            pass


# =========================
# Keep-alive helpers
# =========================
def keep_alive_ping(mac):
    """
    Single ping to keep a specific printer awake.
    """
    printer = create_printer_instance()
    try:
        printer.connect(mac)
        printer.get_status()  # ping
        return True, 'printer_awake'
    except Exception as e:
        return False, str(e)
    finally:
        try:
            printer.disconnect()
        except:
            pass


# =========================
# Endpoints
# =========================
@app.route('/print', methods=['POST'])
def print_photo():
    """Upload and print a photo immediately. Returns error if both printers are busy."""
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if not file or not allowed_file(file.filename):
            return jsonify({'error': 'Invalid file type. Allowed: png, jpg, jpeg, gif, bmp'}), 400

        # Check if both printers are busy/unavailable
        try:
            available_printer, unavailable_error = get_available_printer()
        except Exception as e:
            logger.error(f"Error getting available printer: {e}")
            return jsonify({
                'error': 'Error checking printer availability',
                'details': str(e)
            }), 503

        if available_printer is None:
            # Check if all printers are actively printing (busy)
            active_count = get_active_print_count()
            with active_prints_lock:
                active_info = {mac: job_id for mac, job_id in active_prints.items()}

            # If all printers are actively printing, return busy message
            if active_count >= len(PRINTER_MACS):
                return jsonify({
                    'error': 'All printers are currently busy',
                    'status': 'printing',
                    'active_prints': active_count,
                    'max_printers': len(PRINTER_MACS),
                    'active_jobs': active_info
                }), 503

            # Otherwise, return specific error (no paper, cover open, etc.)
            if unavailable_error:
                return jsonify(unavailable_error), 503
            else:
                return jsonify({
                    'error': 'All printers are currently unavailable',
                    'status': 'unavailable',
                    'active_prints': active_count,
                    'max_printers': len(PRINTER_MACS)
                }), 503

        # Save file
        job_id = create_job_id()
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, f"{job_id}_{filename}")
        file.save(filepath)

        # Start print in background thread
        start_print(job_id, available_printer, filepath, filename)

        return jsonify({
            'message': 'Print job started',
            'job_id': job_id,
            'filename': filename,
            'printer_mac': available_printer,
            'status': 'processing'
        }), 202

    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500




@app.route('/print/pi', methods=['POST'])
def print_photo_to_pi():
    """Forward print request to Raspberry Pi."""
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if file and allowed_file(file.filename):
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
    """Print a photo sent as base64 data. Returns error if both printers are busy."""
    try:
        data = request.get_json()
        if not data or 'image_data' not in data:
            return jsonify({'error': 'No image data provided'}), 400

        # Check if both printers are busy/unavailable
        try:
            available_printer, unavailable_error = get_available_printer()
        except Exception as e:
            logger.error(f"Error getting available printer: {e}")
            return jsonify({
                'error': 'Error checking printer availability',
                'details': str(e)
            }), 503

        if available_printer is None:
            # Check if all printers are actively printing (busy)
            active_count = get_active_print_count()
            with active_prints_lock:
                active_info = {mac: job_id for mac, job_id in active_prints.items()}

            # If all printers are actively printing, return busy message
            if active_count >= len(PRINTER_MACS):
                return jsonify({
                    'error': 'All printers are currently busy',
                    'status': 'printing',
                    'active_prints': active_count,
                    'max_printers': len(PRINTER_MACS),
                    'active_jobs': active_info
                }), 503

            # Otherwise, return specific error (no paper, cover open, etc.)
            if unavailable_error:
                return jsonify(unavailable_error), 503
            else:
                return jsonify({
                    'error': 'All printers are currently unavailable',
                    'status': 'unavailable',
                    'active_prints': active_count,
                    'max_printers': len(PRINTER_MACS)
                }), 503

        job_id = create_job_id()
        image_data_b = base64.b64decode(data['image_data'])

        temp_filename = f"{job_id}_temp_image.jpg"
        temp_filepath = os.path.join(UPLOAD_FOLDER, temp_filename)

        with open(temp_filepath, 'wb') as f:
            f.write(image_data_b)

        # Start print in background thread
        start_print(job_id, available_printer, temp_filepath, 'base64_image.jpg')

        return jsonify({
            'message': 'Print job started',
            'job_id': job_id,
            'printer_mac': available_printer,
            'status': 'processing'
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

        response = requests.post(f'http://{PI_ADDRESS}/print/base64', json=data)
        return jsonify(response.json()), response.status_code

    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Raspberry Pi. Make sure it\'s running the print server.'}), 503
    except Exception as e:
        return jsonify({'error': f'Printing failed: {str(e)}'}), 500


# =========================
# Job endpoints
# =========================
@app.route('/jobs', methods=['GET'])
def list_jobs():
    """List all print jobs."""
    try:
        jobs = get_all_jobs()
        active_count = get_active_print_count()
        with active_prints_lock:
            active_info = {mac: job_id for mac, job_id in active_prints.items()}

        return jsonify({
            'jobs': jobs,
            'active_prints': active_count,
            'active_jobs': active_info,
            'max_printers': len(PRINTER_MACS),
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
    """Cancel a print job (only if not processing)."""
    try:
        job = get_job_status(job_id)
        if job is None:
            return jsonify({'error': 'Job not found'}), 404

        # Check if job is currently printing
        with active_prints_lock:
            printer_mac = None
            for mac, active_job_id in active_prints.items():
                if active_job_id == job_id:
                    printer_mac = mac
                    break

            if printer_mac:
                return jsonify({
                    'error': 'Cannot cancel job that is currently printing',
                    'status': job['status'],
                    'printer_mac': printer_mac
                }), 400

        if job['status'] == 'processing':
            return jsonify({'error': 'Cannot cancel job that is currently processing'}), 400
        elif job['status'] in ['completed', 'failed']:
            return jsonify({'error': 'Cannot cancel job that is already completed or failed'}), 400
        else:
            # Job not found in active prints and not processing - might be a race condition
            return jsonify({'message': 'Job is not currently active'}), 200
    except Exception as e:
        return jsonify({'error': f'Failed to cancel job: {str(e)}'}), 500


# =========================
# Status endpoints
# =========================
@app.route('/status', methods=['GET'])
def printer_status():
    """
    Get printer status.
    - All printers by default
    - Or one via ?mac=AA:BB:...
    """
    try:
        mac = request.args.get('mac')
        macs = [mac] if mac else PRINTER_MACS
        all_status = {}
        for m in macs:
            ok, detail = safe_connect_status(m)
            if ok:
                all_status[m] = {'connected': True, 'status': detail}
            else:
                all_status[m] = {'connected': False, 'error': detail}

        can_print_any = any(s.get('status', {}).get('can_print')
                            for s in all_status.values() if s.get('connected'))
        return jsonify({'printers': all_status, 'any_ready': can_print_any}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/status/pi', methods=['GET'])
def pi_status():
    """Get Raspberry Pi printer status (proxy)."""
    try:
        response = requests.get(f'http://{PI_ADDRESS}/status')
        return jsonify(response.json()), response.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to Raspberry Pi'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =========================
# Settings endpoints (multi)
# =========================
@app.route('/settings', methods=['GET'])
def get_printer_settings():
    """
    Get current printer settings.
    - All printers by default
    - Or one via ?mac=AA:BB:...
    """
    try:
        mac = request.args.get('mac')
        macs = [mac] if mac else PRINTER_MACS
        out = {}
        for m in macs:
            ok, res = get_settings_for_mac(m)
            if ok:
                out[m] = {'connected': True, 'settings': res}
            else:
                out[m] = {'connected': False, 'error': res}
        return jsonify({'printers': out}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/settings/auto-power-off', methods=['POST'])
def set_auto_power_off():
    """
    Set auto power-off minutes on one printer (?mac=..) or all (default).
    Body: {"minutes": 3|5|10}
    """
    try:
        data = request.get_json() or {}
        if 'minutes' not in data:
            return jsonify({'error': 'Minutes parameter required'}), 400

        minutes = data['minutes']
        if minutes not in [3, 5, 10]:
            return jsonify({'error': 'Invalid minutes value. Supported values: 3, 5, 10',
                            'supported_values': [3, 5, 10]}), 400

        mac = request.args.get('mac')
        macs = [mac] if mac else PRINTER_MACS

        results = {}
        for m in macs:
            ok, res = set_auto_power_off_for_mac(m, minutes)
            if ok:
                results[m] = {'success': True, 'setting': minutes, 'result': res}
            else:
                results[m] = {'success': False, 'error': res}

        return jsonify({'message': f'Auto power-off set attempt for {len(macs)} printer(s)',
                        'minutes': minutes, 'results': results}), 200

    except Exception as e:
        return jsonify({'error': f'Failed to set auto power-off: {str(e)}'}), 500


@app.route('/settings/keep-on', methods=['POST'])
def keep_printer_on():
    """
    Set auto power-off to maximum (10 minutes) for one (?mac=..) or all printers.
    """
    try:
        mac = request.args.get('mac')
        macs = [mac] if mac else PRINTER_MACS

        results = {}
        for m in macs:
            ok, res = set_auto_power_off_for_mac(m, 10)
            if ok:
                results[m] = {'success': True, 'setting': 10}
            else:
                results[m] = {'success': False, 'error': res}

        return jsonify({
            'message': 'Applied maximum auto power-off (10 minutes)',
            'results': results,
            'note': 'Ivy 2 still auto-offs after ~10 minutes inactivity.'
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to set keep-on: {str(e)}'}), 500


@app.route('/settings/disable-auto-off', methods=['POST'])
def disable_auto_power_off():
    """
    Ivy 2 doesn’t fully disable auto-off; set to max (10).
    Works for one (?mac=..) or all (default).
    """
    try:
        mac = request.args.get('mac')
        macs = [mac] if mac else PRINTER_MACS

        results = {}
        for m in macs:
            ok, res = set_auto_power_off_for_mac(m, 10)
            if ok:
                results[m] = {'success': True, 'setting': 10}
            else:
                results[m] = {'success': False, 'error': res}

        return jsonify({
            'message': 'Set to maximum auto power-off (10 minutes)',
            'results': results,
            'note': 'Ivy 2 cannot fully disable auto-off. Use keep-alive to extend uptime.'
        }), 200

    except Exception as e:
        return jsonify({'error': f'Failed to configure auto power-off: {str(e)}'}), 500


# =========================
# Keep-alive endpoints (multi)
# =========================
@app.route('/keep-alive', methods=['POST'])
def keep_alive():
    """
    Send one keep-alive ping.
    - All printers by default
    - Or one via ?mac=AA:BB:...
    """
    try:
        mac = request.args.get('mac')
        macs = [mac] if mac else PRINTER_MACS

        results = {}
        for m in macs:
            ok, msg = keep_alive_ping(m)
            if ok:
                results[m] = {'success': True, 'status': msg, 'timestamp': datetime.now().isoformat()}
            else:
                results[m] = {'success': False, 'error': msg}

        return jsonify({'results': results}), 200

    except Exception as e:
        return jsonify({'error': f'Keep-alive failed: {str(e)}'}), 500


@app.route('/keep-alive/start', methods=['POST'])
def start_keep_alive():
    """
    Start background keep-alive pings.
    Body: {"interval": 300} (seconds, default 300)
    Target one via ?mac=.. or all by default.
    """
    try:
        data = request.get_json() or {}
        interval = int(data.get('interval', 300))
        mac = request.args.get('mac')
        macs = [mac] if mac else PRINTER_MACS

        started = {}
        for m in macs:
            if m in keep_alive_threads and keep_alive_threads[m].is_alive():
                started[m] = {'started': False, 'message': 'Already running'}
                continue

            def worker(target_mac):
                while True:
                    ok, _ = keep_alive_ping(target_mac)
                    time.sleep(interval if ok else 60)

            t = threading.Thread(target=worker, args=(m,), daemon=True)
            t.start()
            keep_alive_threads[m] = t
            started[m] = {'started': True, 'interval_seconds': interval}

        return jsonify({'message': 'Keep-alive service status', 'results': started}), 200

    except Exception as e:
        return jsonify({'error': f'Failed to start keep-alive: {str(e)}'}), 500


# =========================
# Health
# =========================
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    try:
        active_count = get_active_print_count()
        with active_prints_lock:
            active_info = {mac: job_id for mac, job_id in active_prints.items()}

        return jsonify({
            'status': 'healthy',
            'service': 'ivy2-printer-api',
            'active_prints': active_count,
            'max_printers': len(PRINTER_MACS),
            'active_jobs': active_info,
            'printers': PRINTER_MACS,
            'keep_alive_running': {
                m: (m in keep_alive_threads and keep_alive_threads[m].is_alive())
                for m in PRINTER_MACS
            }
        }), 200
    except Exception as e:
        return jsonify({'error': f'Health check error: {str(e)}'}), 500


# =========================
# Main
# =========================
if __name__ == '__main__':
    print("Printers:", PRINTER_MACS)
    print(f"Max concurrent prints: {len(PRINTER_MACS)} (one per printer)")

    # Run the Flask app
    print("Starting Ivy2 Printer API server...")
    print("Available endpoints:")
    print("  POST /print - Upload and print an image file (returns 503 if all printers busy)")
    print("  POST /print/pi - Upload and print an image file (via Pi)")
    print("  POST /print/base64 - Print base64 encoded image (returns 503 if all printers busy)")
    print("  POST /print/base64/pi - Print base64 encoded image (via Pi)")
    print("  GET  /jobs - List all print jobs")
    print("  GET  /jobs/<job_id> - Get specific job status")
    print("  DELETE /jobs/<job_id> - Cancel a job (if not processing)")
    print("  GET  /status - Get status for all printers or one via ?mac=..")
    print("  GET  /status/pi - Get Raspberry Pi status")
    print("  GET  /settings - Get settings for all printers or one via ?mac=..")
    print("  POST /settings/auto-power-off - Set auto-off minutes (3,5,10) for all or one via ?mac=..")
    print("  POST /settings/keep-on - Set auto-off to max (10) for all or one via ?mac=..")
    print("  POST /settings/disable-auto-off - Alias of keep-on (Ivy 2 cannot fully disable)")
    print("  POST /keep-alive - One ping (all or ?mac=..)")
    print("  POST /keep-alive/start - Start background pings (all or ?mac=..) {interval: seconds}")
    print("  GET  /health - Health + active print state")
    print(f"\nPi address: {PI_ADDRESS}")
    print("\nServer will start on http://0.0.0.0:5000")

    app.run(host='0.0.0.0', port=5000, debug=True)
