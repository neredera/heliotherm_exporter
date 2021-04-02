# heliotherm_exporter

This is a [Prometheus exporter](https://prometheus.io/docs/instrumenting/exporters/) for [Heliotherm heat pumps](https://www.heliotherm.com/produkte/waermepumpen/).

It allows to show the status (temperatures, cycling, etc.) of your heat pump into Prometheus.

## Usage

Clone the respoitory und install with:
```bash
git clone https://github.com/neredera/heliotherm_exporter.git
cd heliotherm_exporter
.\setup.sh
```

Enter the hostname or IP adress in `heliotherm_exporter.service` (at the moment only connection via a serial/TCP gateway like a Moxa DE-311 is supported):
```bash
nano heliotherm_exporter.service

sudo systemctl daemon-reload
sudo systemctl restart heliotherm_exporter.service
sudo systemctl status heliotherm_exporter.service
```

Command line parameters:
```bash
> python3 exporter.py --help

usage: exporter.py [-h] [--port PORT] [--lan_gateway LAN_GATEWAY]
                   [--lan_gateway_port LAN_GATEWAY_PORT]

optional arguments:
  -h, --help            show this help message and exit
  --port PORT           The port where to expose the exporter (default:9997)
  --lan_gateway LAN_GATEWAY
                        Hostname or IP of the LAN to serial gateway
  --lan_gateway_port LAN_GATEWAY_PORT
                        TCP port for the LAN gateway (default:4001)
```

## Usage with docker

Example `docker-compose.yml`:
```
version: '3.4'

services:
  heliotherm-exporter:
    image: neredera/heliotherm-exporter:latest
    restart: always
    command: " --lan_gateway LAN_GATEWAY"
    ports:
      - 9997:9997
```

## Prometheus metrics

Example how to add the exporter to the prometheus configuration (`prometheus.yml`):
```yml
  - job_name: heliotherm
    scrape_interval: 30s
    static_configs:
    - targets: ['heliotherm-exporter-host.local:9997']
```

For a sample dashboard see: TODO


