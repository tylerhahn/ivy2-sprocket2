from sprocket2 import Sprocket2Printer
import image

PRINTER_MAC = "XX:XX:XX:XX:XX:XX"  # Replace with your HP Sprocket 2 MAC address


def print_photo():
    """Print a photo using the HP Sprocket 2 printer."""
    printer = Sprocket2Printer()

    try:
        printer.connect(PRINTER_MAC)
        print("Connected to HP Sprocket 2 printer")

        # Print an image
        printer.print("./assets/test_image.jpg")
        print("Print job sent successfully!")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        printer.disconnect()
        print("Disconnected from printer")


def preview_image(image_path, output_path="sprocket2_preview_image.jpeg"):
    """Get a preview of what the printed image will look like on HP Sprocket 2."""
    image_data = image.prepare_image_sprocket2(image_path, True, 100, True)

    with open(output_path, "wb") as file:
        file.write(image_data)

    print(f"Preview saved as {output_path}")


def check_printer_status():
    """Check the status of the HP Sprocket 2 printer."""
    printer = Sprocket2Printer()

    try:
        printer.connect(PRINTER_MAC)

        # Get printer status
        status = printer.get_status()
        print(f"Printer status: {status}")

        # Get printer settings
        settings = printer.get_setting()
        print(f"Printer settings: {settings}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        printer.disconnect()


if __name__ == '__main__':
    print("HP Sprocket 2 Printer Example")
    print("1. Print photo")
    print("2. Preview image")
    print("3. Check printer status")

    choice = input("Enter your choice (1-3): ")

    if choice == "1":
        print_photo()
    elif choice == "2":
        image_path = input("Enter image path: ")
        preview_image(image_path)
    elif choice == "3":
        check_printer_status()
    else:
        print("Invalid choice")