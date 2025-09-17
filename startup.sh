#!/bin/bash
cd /home/blizzard/Documents/repos/ivy2-sprocket2

#Activate new
source venv/bin/activate

#set bluetooth mode
sudo hciconfig hci0 sspmode 0

#launch printer service
python launch_printer.py
