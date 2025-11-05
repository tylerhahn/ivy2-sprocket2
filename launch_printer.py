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

# =========================
# Printer & server config
# =========================
# Put your printer MACs here (two or more supported)
PRINTER_MACS = [
    "10:23:81:44:C1:CD",  # Printer A
    "10:23:81:44:C1:CE",  # Printer B (update to your second MAC)
]

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}
PI_ADDRESS = "192.168.1.55:5000"

# Create uploads directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========================
# Shared job queue & state
# =========================
print_queue = queue.Queue()
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
            'status': status,  # 'queued', 'processing', 'completed', 'failed', 'cancelled'
            'message': message,
            'progress': progress,
            'timestamp': job_status.get(job_id, {}).get('timestamp', datetime.now().isoformat()),
            'updated_at': datetime.now().isoformat(),
            **(extra or {})
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
    printer = create_printer_instance()
    try:
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
    except Exception as e:
        return False, str(e)
    finally:
        try:
            printer.disconnect()
        except:
            pass


def check_printer_ready(printer_mac):
    ok, detail = safe_connect_status(printer_mac)
    if not ok:
        return False, f"Unavailable: {detail}"
    return detail.get('can_print', False), "Ready" if detail.get('can_print') else "Not ready"


def choose_available_printer():
    """Return the first MAC that is currently ready; else None."""
    for mac in PRINTER_MACS:
        ready, _ = check_printer_ready(mac)
        if ready:
            return mac
    return None


# =========================
# Worker: one per printer
# =========================
def print_worker(printer_mac):
    """
    Dedicated worker for one printer. Pulls jobs from a shared queue.
    If its printer isn't ready, it requeues the job so another worker can grab it.
    """
    while True:
        job_data = None
        try:
            job_data = print_queue.get(timeout=1)
        except queue.Empty:
            continue

        try:
            job_id = job_data['job_id']
            filepath = job_data['filepath']
            filename = job_data['filename']
            attempts = job_data.get('attempts', 0)

            # Quick readiness check
            ready, reason = check_printer_ready(printer_mac)
            if not ready:
                # Put job back for another worker to attempt
                job_data['attempts'] = attempts + 1
                update_job_status(
                    job_id,
                    'queued',
                    f'Printer {printer_mac} unavailable ({reason}). Requeued.',
                    extra={'attempts': job_data['attempts']}
                )
                print_queue.put(job_data)
                time.sleep(0.8)  # let other workers contend
                continue

            update_job_status(job_id, 'processing',
                              f'Using printer {printer_mac}: connecting...',
                              15, extra={'printer_mac': printer_mac})

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
                    raise Exception("No paper in printer")
                if is_wrong_smart_sheet:
                    raise Exception("Wrong smart sheet detected")

                update_job_status(job_id, 'processing', 'Printing image...', 60)
                printer.print(filepath)

                if os.path.exists(filepath):
                    os.remove(filepath)

                update_job_status(job_id, 'completed', f'Printed on {printer_mac}', 100)

            except Exception as e:
                # Clean up file even if printing fails
                if os.path.exists(filepath):
                    os.remove(filepath)
                update_job_status(job_id, 'failed',
                                  f'Print failed on {printer_mac}: {str(e)}', 0)
            finally:
                try:
                    printer.disconnect()
                except:
                    pass

        except Exception as e:
            if job_data and 'job_id' in job_data:
                update_job_status(job_data['job_id'], 'failed', f'Worker error: {str(e)}', 0)
        finally:
            try:
                print_queue.task_done()
            except:
                pass


def start_print_workers():
    """Start one worker thread per configured printer MAC."""
    threads = []
    for mac in PRINTER_MACS:
        t = threading.Thread(target=print_worker, args=(mac,), daemon=True)
        t.start()
        threads.append(t)
    return threads


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
    """Upload and queue a photo for printing (distributed across printers)."""
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if file and allowed_file(file.filename):
            job_id = create_job_id()
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, f"{job_id}_{filename}")
            file.save(filepath)

            job_data = {
                'job_id': job_id,
                'filepath': filepath,
                'filename': filename,
                'created_at': datetime.now().isoformat(),
                'attempts': 0,
                'preferred_printer': None  # reserved for future targeting
            }

            print_queue.put(job_data)
            update_job_status(job_id, 'queued', 'Job added to print queue', 0, extra={'attempts': 0})

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
    """Try to print a photo immediately on any ready printer; otherwise queue it."""
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400

        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            chosen_mac = choose_available_printer()
            if not chosen_mac:
                # Fall back to queue
                job_id = create_job_id()
                job_data = {
                    'job_id': job_id,
                    'filepath': filepath,
                    'filename': filename,
                    'created_at': datetime.now().isoformat(),
                    'attempts': 0
                }
                print_queue.put(job_data)
                update_job_status(job_id, 'queued', 'No printers immediately ready; queued for next available.', 0)
                return jsonify({
                    'message': 'No printers ready; job queued',
                    'job_id': job_id,
                    'queue_position': print_queue.qsize()
                }), 202

            printer = create_printer_instance()
            try:
                printer.connect(chosen_mac)
                printer.print(filepath)
                # Clean up uploaded file
                if os.path.exists(filepath):
                    os.remove(filepath)
                return jsonify({'message': 'Photo printed successfully',
                                'filename': filename,
                                'printer_mac': chosen_mac}), 200
            except Exception as e:
                # On failure, queue it
                job_id = create_job_id()
                job_data = {
                    'job_id': job_id,
                    'filepath': filepath,
                    'filename': filename,
                    'created_at': datetime.now().isoformat(),
                    'attempts': 0
                }
                print_queue.put(job_data)
                update_job_status(job_id, 'queued', f'Immediate failed on {chosen_mac}: {str(e)}; job queued.', 0)
                return jsonify({'message': 'Immediate print failed; job queued', 'job_id': job_id}), 202
            finally:
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
    """Queue a photo that is sent as base64 data."""
    try:
        data = request.get_json()
        if not data or 'image_data' not in data:
            return jsonify({'error': 'No image data provided'}), 400

        job_id = create_job_id()
        image_data_b = base64.b64decode(data['image_data'])

        temp_filename = f"{job_id}_temp_image.jpg"
        temp_filepath = os.path.join(UPLOAD_FOLDER, temp_filename)

        with open(temp_filepath, 'wb') as f:
            f.write(image_data_b)

        job_data = {
            'job_id': job_id,
            'filepath': temp_filepath,
            'filename': 'base64_image.jpg',
            'created_at': datetime.now().isoformat(),
            'attempts': 0
        }
        print_queue.put(job_data)
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
        while not print_queue.empty():
            try:
                print_queue.get_nowait()
                print_queue.task_done()
            except queue.Empty:
                break

        with job_lock:
            for job_id, job_data in job_status.items():
                if job_data['status'] == 'queued':
                    job_data['status'] = 'cancelled'
                    job_data['message'] = 'Job cancelled due to queue clear'
                    job_data['updated_at'] = datetime.now().isoformat()

        return jsonify({'message': 'Queue cleared successfully'}), 200
    except Exception as e:
        return jsonify({'error': f'Failed to clear queue: {str(e)}'}), 500


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
        return jsonify({
            'status': 'healthy',
            'service': 'ivy2-printer-api',
            'queue_size': print_queue.qsize(),
            'active_jobs': len([j for j in job_status.values() if j['status'] in ['queued', 'processing']]),
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
    # Start the print workers (one per printer)
    print("Starting print worker threads...")
    start_print_workers()
    print("Printers:", PRINTER_MACS)

    # Run the Flask app
    print("Starting Ivy2 Printer API server...")
    print("Available endpoints:")
    print("  POST /print - Upload and print an image file (queued, multi-printer)")
    print("  POST /print/immediate - Upload and print immediately (or queue) on any ready printer")
    print("  POST /print/pi - Upload and print an image file (via Pi)")
    print("  POST /print/base64 - Print base64 encoded image (queued)")
    print("  POST /print/base64/pi - Print base64 encoded image (via Pi)")
    print("  GET  /jobs - List all print jobs")
    print("  GET  /jobs/<job_id> - Get specific job status")
    print("  DELETE /jobs/<job_id> - Cancel a queued job")
    print("  POST /queue/clear - Clear all queued jobs")
    print("  GET  /status - Get status for all printers or one via ?mac=..")
    print("  GET  /status/pi - Get Raspberry Pi status")
    print("  GET  /settings - Get settings for all printers or one via ?mac=..")
    print("  POST /settings/auto-power-off - Set auto-off minutes (3,5,10) for all or one via ?mac=..")
    print("  POST /settings/keep-on - Set auto-off to max (10) for all or one via ?mac=..")
    print("  POST /settings/disable-auto-off - Alias of keep-on (Ivy 2 cannot fully disable)")
    print("  POST /keep-alive - One ping (all or ?mac=..)")
    print("  POST /keep-alive/start - Start background pings (all or ?mac=..) {interval: seconds}")
    print("  GET  /health - Health + keep-alive state")
    print(f"\nPi address: {PI_ADDRESS}")
    print("\nServer will start on http://0.0.0.0:5000")

    app.run(host='0.0.0.0', port=5000, debug=True)
