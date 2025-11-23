import time
import queue
from loguru import logger

from task import (
    StartSessionTask,
    GetStatusTask,
    GetSettingTask,
    SetSettingTask,
    GetPrintReadyTask,
    RebootTask
)
import image

from exceptions import (
    ClientUnavailableError,
    ReceiveTimeoutError,
    AckError,
    LowBatteryError,
    CoverOpenError,
    NoPaperError,
    WrongSmartSheetError
)
from client import ClientThread
from utils import parse_incoming_message

PRINT_BATTERY_MIN = 10  # Lowered from 30 to 10 for testing
PRINT_DATA_CHUNK = 990


class Ivy2Printer:
    def __init__(self):
        self.client = ClientThread()

    def connect(self, mac_address, port=1):
        self.client.connect(mac_address, port)
        battery_level, mtu = self.__start_session()

        logger.debug("Connected; Battery level: {}; MTU: {}".format(battery_level, mtu))

    def disconnect(self):
        self.client.disconnect()

    def is_connected(self):
        return self.client.alive.is_set()

    def print(self, target, auto_crop=True, transfer_timeout=60):
        image_data = bytes()

        if type(target) is str:
            image_data = image.prepare_image(target, auto_crop)
        elif type(target) is bytes:
            image_data = target
        else:
            raise ValueError(
                "Unsupported target; expected string or bytes but got {}".format(
                    type(target)
                )
            )

        image_length = len(image_data)

        self.check_print_worthiness()
        self.get_setting()

        # setup the printer to receive the image data
        self.get_print_ready(image_length)

        # Calculate dynamic timeout based on image size
        # Estimate: ~0.02s per chunk + buffer for processing
        num_chunks = (image_length + PRINT_DATA_CHUNK - 1) // PRINT_DATA_CHUNK
        estimated_transfer_time = num_chunks * 0.02 + 10  # 10s buffer
        dynamic_timeout = max(transfer_timeout, int(estimated_transfer_time * 2))

        logger.debug(f"Image size: {image_length} bytes, {num_chunks} chunks, timeout: {dynamic_timeout}s")

        # split up the image and add to the client queue
        start_index = 0
        while True:
            end_index = min(start_index + PRINT_DATA_CHUNK, image_length)
            image_chunk = image_data[start_index:end_index]

            self.client.outbound_q.put(image_chunk)

            if end_index >= image_length:
                break

            start_index = end_index

        logger.debug("Beginning data transfer...")

        # Wait for queue to be mostly empty (give it time to send chunks)
        # This ensures we don't wait for ack before data is actually sent
        queue_wait_start = time.time()
        while not self.client.outbound_q.empty() and (time.time() - queue_wait_start) < 5:
            time.sleep(0.1)

        # wait longer than usual since the transfer takes some time
        self.__receive_message(dynamic_timeout)

        logger.debug("Data transfer complete! Printing should begin in a moment")

    def reboot(self):
        return self.__perform_task(RebootTask())

    def get_status(self):
        return self.__perform_task(GetStatusTask())

    def wait_for_print_complete(self, max_wait_time=120, poll_interval=2, min_wait_time=30):
        """
        Poll the printer status until printing is complete.
        Returns True if print completed successfully, False if timeout or error.

        This checks the printer status periodically to see if it's still printing.
        The printer may still be physically printing even after data transfer completes.

        Strategy:
        1. Wait minimum time (min_wait_time) to allow physical printing to start
        2. Poll status periodically
        3. Consider print complete when we get consecutive "ready" statuses
        """
        start_time = time.time()
        consecutive_ready = 0  # Count consecutive "ready" status checks
        required_ready_checks = 3  # Number of consecutive ready checks needed

        # First, wait minimum time for physical printing to start
        logger.debug(f"Waiting {min_wait_time}s minimum for physical printing to start...")
        time.sleep(min_wait_time)

        while (time.time() - start_time) < max_wait_time:
            try:
                status = self.get_status()
                error_code, battery_level, _, is_cover_open, is_no_paper, is_wrong_smart_sheet = status

                # If there are critical errors, the print might have failed
                if error_code != 0:
                    logger.debug(f"Printer status shows error code: {error_code} (may be transient)")

                # Check if printer is in a ready state (no blocking conditions)
                # If we get consecutive ready statuses, assume printing is done
                is_ready = (battery_level >= 10 and not is_cover_open and
                           not is_no_paper and not is_wrong_smart_sheet)

                if is_ready and error_code == 0:
                    consecutive_ready += 1
                    logger.debug(f"Printer ready check {consecutive_ready}/{required_ready_checks}")
                    if consecutive_ready >= required_ready_checks:
                        elapsed = time.time() - start_time
                        logger.debug(f"Print appears complete after {elapsed:.1f}s (printer ready)")
                        return True
                else:
                    consecutive_ready = 0  # Reset counter if not ready
                    if is_cover_open or is_no_paper or is_wrong_smart_sheet:
                        logger.debug(f"Printer not ready: cover_open={is_cover_open}, "
                                   f"no_paper={is_no_paper}, wrong_sheet={is_wrong_smart_sheet}")

                time.sleep(poll_interval)

            except Exception as e:
                logger.warning(f"Error checking print status: {e}")
                consecutive_ready = 0  # Reset on error
                # Continue polling despite errors
                time.sleep(poll_interval)

        elapsed = time.time() - start_time
        logger.warning(f"Timeout waiting for print to complete after {elapsed:.1f}s")
        # Return True anyway if we've waited a reasonable amount - the print might be done
        # but we just couldn't confirm via status checks
        if elapsed >= min_wait_time + 30:
            logger.info("Assuming print complete after reasonable wait time")
            return True
        return False

    def get_setting(self):
        return self.__perform_task(GetSettingTask())

    def set_setting(self, auto_power_off):
        """Sets the auto power off setting on the printer.

        auto_power_off: Time in minutes before the printer turns off without any
        activity. Supported values are 3, 5, and 10.
        """
        return self.__perform_task(SetSettingTask(auto_power_off))

    def get_print_ready(self, length):
        return self.__perform_task(GetPrintReadyTask(length))

    def check_print_worthiness(self):
        status = self.get_status()
        error_code, battery_level, _, is_cover_open, is_no_paper, is_wrong_smart_sheet = status

        if error_code != 0:
            logger.error(
                "Status contains a non-zero error code: {}",
                error_code
            )

        if battery_level < PRINT_BATTERY_MIN:
            raise LowBatteryError()

        if is_cover_open:
            raise CoverOpenError()

        if is_no_paper:
            raise NoPaperError()

        if is_wrong_smart_sheet:
            raise WrongSmartSheetError()

    def __start_session(self):
        return self.__perform_task(StartSessionTask())

    def __perform_task(self, task):
        # send the task's message
        self.__send_message(task.get_message())
        response = self.__receive_message()

        if response[2] != task.ack:
            raise AckError("Got invalid ack; expected {} but got {}".format(
                task.ack, response[3]
            ))

        # process and return the response
        return task.process_response(response)

    def __send_message(self, message):
        if not self.client.alive.is_set():
            raise ClientUnavailableError()

        # add the message to the client thread's outbound queue
        self.client.outbound_q.put(message)

    def __receive_message(self, timeout=5):
        start = int(time.time())
        while int(time.time()) < (start + timeout):
            if not self.client.alive.is_set():
                raise ClientUnavailableError("")

            try:
                # attempt to read the client thread's inbound queue
                response = parse_incoming_message(
                    self.client.inbound_q.get(False, 0.1)
                )

                logger.debug(
                    "Received message: ack: {}, error: {}",
                    response[2],
                    response[3]
                )
                return response
            except queue.Empty:
                pass

        raise ReceiveTimeoutError()
