#!/bin/bash

# go to current directory
cd "${0%/*}"

# install needed packages
sudo apt-get install git python3-venv python3-pip

# activate a virtual environment
python3 -m venv .

# install python modules
python3 -m pip install prometheus_client pyserial

# user for service
useradd -Mr heliotherm_exporter
usermod -L heliotherm_exporter
# usermod -aG root heliotherm_exporter
# usermod -aG sudo heliotherm_exporter

chmod +x exporter.py

# sudo systemctl daemon-reload

sudo systemctl enable $(pwd)/heliotherm_exporter.service

sudo systemctl start heliotherm_exporter.service

python3 exporter.py --help

sudo systemctl status heliotherm_exporter.service
