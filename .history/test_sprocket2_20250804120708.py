#!/usr/bin/env python3
"""
Test script for HP Sprocket 2 printer support.
This script tests basic functionality without requiring a physical printer.
"""

import os
import sys
from PIL import Image
import image

def test_image_preparation():
    """Test the image preparation function for HP Sprocket 2."""
    print("Testing image preparation for HP Sprocket 2...")
    
    # Create a test image if assets directory doesn't exist
    if not os.path.exists("assets"):
        os.makedirs("assets")
    
    test_image_path = "assets/test_image.jpg"
    
    # Create a simple test image if it doesn't exist
    if not os.path.exists(test_image_path):
        print("Creating test image...")
        test_image = Image.new('RGB', (800, 600), color='red')
        test_image.save(test_image_path)
        print(f"Created test image: {test_image_path}")
    
    try:
        # Test the image preparation function
        image_data = image.prepare_image_sprocket2(test_image_path, auto_crop=True)
        print(f"✓ Image preparation successful")
        print(f"  - Image data size: {len(image_data)} bytes")
        
        # Save a preview
        preview_path = "sprocket2_test_preview.jpeg"
        with open(preview_path, "wb") as f:
            f.write(image_data)
        print(f"  - Preview saved as: {preview_path}")
        
        return True
        
    except Exception as e:
        print(f"✗ Image preparation failed: {e}")
        return False

def test_imports():
    """Test that all required modules can be imported."""
    print("Testing imports...")
    
    try:
        from sprocket2 import Sprocket2Printer
        print("✓ Sprocket2Printer imported successfully")
        
        from sprocket2_task import StartSessionTask, GetStatusTask
        print("✓ Sprocket2Task modules imported successfully")
        
        from client import ClientThread
        print("✓ ClientThread imported successfully")
        
        from exceptions import ClientUnavailableError
        print("✓ Exceptions imported successfully")
        
        return True
        
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False

def test_basic_functionality():
    """Test basic functionality without connecting to a printer."""
    print("Testing basic functionality...")
    
    try:
        from sprocket2 import Sprocket2Printer
        
        # Create printer instance
        printer = Sprocket2Printer()
        print("✓ Printer instance created")
        
        # Test connection state
        connected = printer.is_connected()
        print(f"✓ Connection state check: {connected}")
        
        return True
        
    except Exception as e:
        print(f"✗ Basic functionality test failed: {e}")
        return False

def main():
    """Run all tests."""
    print("HP Sprocket 2 Printer Test Suite")
    print("=" * 40)
    
    tests = [
        ("Import Test", test_imports),
        ("Basic Functionality Test", test_basic_functionality),
        ("Image Preparation Test", test_image_preparation),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n{test_name}:")
        if test_func():
            passed += 1
            print(f"✓ {test_name} PASSED")
        else:
            print(f"✗ {test_name} FAILED")
    
    print(f"\n{'=' * 40}")
    print(f"Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! The HP Sprocket 2 support is ready.")
        print("\nNext steps:")
        print("1. Pair your HP Sprocket 2 printer via Bluetooth")
        print("2. Update the MAC address in sprocket2_example.py")
        print("3. Run: python sprocket2_example.py")
    else:
        print("❌ Some tests failed. Please check the errors above.")
        sys.exit(1)

if __name__ == "__main__":
    main() 