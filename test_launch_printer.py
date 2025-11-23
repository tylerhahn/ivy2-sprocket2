#!/usr/bin/env python3
"""
Test suite for the queue-less print system.
Tests that the system properly handles max 2 concurrent prints (one per printer).
"""

import os
import sys
import json
import base64
import threading
import time
import unittest
from unittest.mock import Mock, MagicMock, patch
from io import BytesIO
from PIL import Image

# Add current directory to path to import launch_printer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the Flask app and necessary components
from launch_printer import app, PRINTER_MACS, active_prints, active_prints_lock, job_status, job_lock


class TestPrintSystem(unittest.TestCase):
    """Test suite for the queue-less print system."""
    
    def setUp(self):
        """Set up test fixtures before each test."""
        self.client = app.test_client()
        self.app = app
        
        # Clear active prints and job status before each test
        with active_prints_lock:
            active_prints.clear()
        with job_lock:
            job_status.clear()
        
        # Ensure uploads directory exists
        os.makedirs('uploads', exist_ok=True)
    
    def create_test_image(self, filename='test_image.jpg'):
        """Create a test image file."""
        img = Image.new('RGB', (100, 100), color='red')
        img_path = os.path.join('uploads', filename)
        img.save(img_path)
        return img_path
    
    def create_test_image_bytes(self):
        """Create test image bytes."""
        img = Image.new('RGB', (100, 100), color='red')
        buffer = BytesIO()
        img.save(buffer, format='JPEG')
        return buffer.getvalue()
    
    @patch('launch_printer.Ivy2Printer')
    @patch('launch_printer.check_printer_ready')
    @patch('launch_printer.start_print')
    def test_print_success_when_printer_available(self, mock_start_print, mock_check_ready, mock_printer_class):
        """Test that a print request succeeds when a printer is available."""
        # Mock printer available
        mock_check_ready.return_value = (True, "Ready")
        
        # Create test image
        img_bytes = self.create_test_image_bytes()
        
        # Make print request
        response = self.client.post(
            '/print',
            data={'image': (BytesIO(img_bytes), 'test.jpg')},
            content_type='multipart/form-data'
        )
        
        # Assert response
        assert response.status_code == 202
        data = json.loads(response.data)
        assert data['status'] == 'processing'
        assert 'job_id' in data
        assert 'printer_mac' in data
        assert data['printer_mac'] in PRINTER_MACS
        
        # Verify start_print was called
        assert mock_start_print.called
    
    @patch('launch_printer.check_printer_ready')
    def test_print_fails_when_both_printers_busy(self, mock_check_ready):
        """Test that a print request returns 503 when both printers are busy."""
        # Mark both printers as busy
        with active_prints_lock:
            active_prints[PRINTER_MACS[0]] = 'job-1'
            active_prints[PRINTER_MACS[1]] = 'job-2'
        
        # Mock printer check - should check readiness but both are busy
        mock_check_ready.return_value = (True, "Ready")
        
        # Create test image
        img_bytes = self.create_test_image_bytes()
        
        # Make print request
        response = self.client.post(
            '/print',
            data={'image': (BytesIO(img_bytes), 'test.jpg')},
            content_type='multipart/form-data'
        )
        
        # Assert 503 response
        assert response.status_code == 503
        data = json.loads(response.data)
        assert data['status'] == 'printing'
        assert data['error'] == 'All printers are currently busy'
        assert data['active_prints'] == 2
        assert data['max_printers'] == 2
        assert len(data['active_jobs']) == 2
    
    @patch('launch_printer.Ivy2Printer')
    @patch('launch_printer.check_printer_ready')
    @patch('launch_printer.start_print')
    def test_print_base64_success(self, mock_start_print, mock_check_ready, mock_printer_class):
        """Test that a base64 print request succeeds when a printer is available."""
        # Mock printer available
        mock_check_ready.return_value = (True, "Ready")
        
        # Create test image and encode to base64
        img_bytes = self.create_test_image_bytes()
        img_base64 = base64.b64encode(img_bytes).decode('utf-8')
        
        # Make print request
        response = self.client.post(
            '/print/base64',
            json={'image_data': img_base64},
            content_type='application/json'
        )
        
        # Assert response
        assert response.status_code == 202
        data = json.loads(response.data)
        assert data['status'] == 'processing'
        assert 'job_id' in data
        assert 'printer_mac' in data
        
        # Verify start_print was called
        assert mock_start_print.called
    
    @patch('launch_printer.check_printer_ready')
    def test_print_base64_fails_when_both_printers_busy(self, mock_check_ready):
        """Test that a base64 print request returns 503 when both printers are busy."""
        # Mark both printers as busy
        with active_prints_lock:
            active_prints[PRINTER_MACS[0]] = 'job-1'
            active_prints[PRINTER_MACS[1]] = 'job-2'
        
        # Mock printer check
        mock_check_ready.return_value = (True, "Ready")
        
        # Create test image and encode to base64
        img_bytes = self.create_test_image_bytes()
        img_base64 = base64.b64encode(img_bytes).decode('utf-8')
        
        # Make print request
        response = self.client.post(
            '/print/base64',
            json={'image_data': img_base64},
            content_type='application/json'
        )
        
        # Assert 503 response
        assert response.status_code == 503
        data = json.loads(response.data)
        assert data['status'] == 'printing'
        assert data['error'] == 'All printers are currently busy'
    
    def test_print_fails_with_invalid_file_type(self):
        """Test that print request fails with invalid file type."""
        response = self.client.post(
            '/print',
            data={'image': (BytesIO(b'not an image'), 'test.txt')},
            content_type='multipart/form-data'
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data
        assert 'Invalid file type' in data['error']
    
    def test_print_fails_without_image(self):
        """Test that print request fails without image file."""
        response = self.client.post('/print')
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data
        assert 'No image file provided' in data['error']
    
    @patch('launch_printer.check_printer_ready')
    def test_concurrent_print_capacity(self, mock_check_ready):
        """Test that exactly 2 prints can run concurrently (one per printer)."""
        # Mock printer available
        mock_check_ready.return_value = (True, "Ready")
        
        img_bytes = self.create_test_image_bytes()
        
        # First print should succeed
        response1 = self.client.post(
            '/print',
            data={'image': (BytesIO(img_bytes), 'test1.jpg')},
            content_type='multipart/form-data'
        )
        assert response1.status_code == 202
        data1 = json.loads(response1.data)
        printer1 = data1['printer_mac']
        
        # Small delay to let first print start
        time.sleep(0.1)
        
        # Second print should succeed (different printer)
        response2 = self.client.post(
            '/print',
            data={'image': (BytesIO(img_bytes), 'test2.jpg')},
            content_type='multipart/form-data'
        )
        assert response2.status_code == 202
        data2 = json.loads(response2.data)
        printer2 = data2['printer_mac']
        
        # Verify they're using different printers
        assert printer1 != printer2
        assert printer1 in PRINTER_MACS
        assert printer2 in PRINTER_MACS
        
        # Third print should fail (both printers busy)
        response3 = self.client.post(
            '/print',
            data={'image': (BytesIO(img_bytes), 'test3.jpg')},
            content_type='multipart/form-data'
        )
        assert response3.status_code == 503
    
    def test_get_jobs_shows_active_prints(self):
        """Test that GET /jobs shows active prints information."""
        # Add some active prints
        with active_prints_lock:
            active_prints[PRINTER_MACS[0]] = 'test-job-1'
        
        # Add some job statuses
        with job_lock:
            job_status['test-job-1'] = {
                'status': 'processing',
                'message': 'Printing...',
                'progress': 50
            }
        
        response = self.client.get('/jobs')
        assert response.status_code == 200
        data = json.loads(response.data)
        
        assert 'active_prints' in data
        assert data['active_prints'] == 1
        assert data['max_printers'] == 2
        assert 'active_jobs' in data
        assert PRINTER_MACS[0] in data['active_jobs']
    
    def test_get_job_status(self):
        """Test getting status of a specific job."""
        job_id = 'test-job-123'
        
        # Add job status
        with job_lock:
            job_status[job_id] = {
                'status': 'processing',
                'message': 'Printing...',
                'progress': 75
            }
        
        response = self.client.get(f'/jobs/{job_id}')
        assert response.status_code == 200
        data = json.loads(response.data)
        
        assert data['job_id'] == job_id
        assert data['status']['status'] == 'processing'
        assert data['status']['progress'] == 75
    
    def test_get_nonexistent_job(self):
        """Test getting status of nonexistent job."""
        response = self.client.get('/jobs/nonexistent-job-id')
        assert response.status_code == 404
        data = json.loads(response.data)
        assert 'error' in data
        assert 'not found' in data['error'].lower()
    
    def test_health_endpoint_shows_active_prints(self):
        """Test that /health endpoint shows active print information."""
        # Add an active print
        with active_prints_lock:
            active_prints[PRINTER_MACS[0]] = 'test-job-1'
        
        response = self.client.get('/health')
        assert response.status_code == 200
        data = json.loads(response.data)
        
        assert data['status'] == 'healthy'
        assert 'active_prints' in data
        assert data['active_prints'] == 1
        assert data['max_printers'] == 2
        assert 'active_jobs' in data
    
    @patch('launch_printer.check_printer_ready')
    def test_available_printer_selection(self, mock_check_ready):
        """Test that get_available_printer selects unused printers."""
        from launch_printer import get_available_printer
        
        mock_check_ready.return_value = (True, "Ready")
        
        # Both printers should be available
        printer1 = get_available_printer()
        self.assertIn(printer1, PRINTER_MACS)
        
        # Mark first printer as busy
        with active_prints_lock:
            active_prints[printer1] = 'job-1'
        
        # Second printer should be selected
        printer2 = get_available_printer()
        self.assertIn(printer2, PRINTER_MACS)
        self.assertNotEqual(printer1, printer2)
        
        # Mark second printer as busy
        with active_prints_lock:
            active_prints[printer2] = 'job-2'
        
        # No printer should be available
        printer3 = get_available_printer()
        self.assertIsNone(printer3)


if __name__ == '__main__':
    # Clean up any test files before running
    if os.path.exists('uploads'):
        # Only clean test files, not all uploads
        for f in os.listdir('uploads'):
            if f.startswith('test_') or 'temp_image' in f:
                try:
                    os.remove(os.path.join('uploads', f))
                except:
                    pass
    
    # Run tests using unittest
    unittest.main(verbosity=2)
